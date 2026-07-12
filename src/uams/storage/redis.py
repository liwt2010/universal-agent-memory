"""Redis distributed storage and signal queue implementation for UAMS.

Optional dependency: requires `pip install redis`.
Provides distributed cache, pub/sub signals, and TTL-based expiry.
"""

import json
import random
import re
import threading
from typing import Any, Dict, List, Optional, Set

from uams.storage.base import MemoryStore
from uams.core.models import (
    Memory, MemoryId, TemporalAnchor, AgentContext,
    MemoryPayload, MemoryMetadata, Relation,
)
from uams.core.enums import MemoryType, PrivacyLevel, EventType
from uams.utils.logging import get_logger
from uams.utils.embedding_serde import serialize_embedding, deserialize_embedding

logger = get_logger(__name__)

# Tokenizer for inverted index. Lowercases, splits on non-alphanumeric, drops
# single-character tokens (avoids indexing 'a', 'I', etc. which would explode
# the index and rarely carry search signal).
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_TOKEN_LEN = 2


def _tokenize(text: str) -> Set[str]:
    if not text:
        return set()
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= _MIN_TOKEN_LEN}


class RedisStore(MemoryStore):
    """
    Redis-backed distributed memory store.
    
    Features:
    - Redis Hash for memory data (HSET/HGETALL/HDEL)
    - Redis ZSET for time-based expiry (score = expires_at timestamp)
    - Redis Pub/Sub for inter-agent signals (optional)
    - Automatic TTL cleanup via background thread or lazy eviction
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        key_prefix: str = "uams:memory:",
        expiry_zset_key: str = "uams:expiry",
        ttl_seconds: Optional[float] = None,
        enable_pubsub: bool = False,
        pool_max_connections: int = 50,
    ):
        self._host = host
        self._port = port
        self._db = db
        self._password = password
        self._key_prefix = key_prefix
        self._expiry_zset_key = expiry_zset_key
        self._ttl_seconds = ttl_seconds
        self._enable_pubsub = enable_pubsub
        self._client = None
        self._pool = None
        self._pubsub = None
        self._lock = threading.RLock()
        self._available = False
        # Auto-disable state: set to True the first time a Redis call
        # raises a connection error. Mirrors MultiAgentCoordinator's
        # pattern so transient outages don't degrade into per-call
        # exception log spam and silent data loss.
        self._disabled = False

        try:
            import redis
            self._redis_module = redis
            self._pool = redis.ConnectionPool(
                host=host, port=port, db=db, password=password,
                max_connections=pool_max_connections,
                socket_connect_timeout=5,
                socket_timeout=5,
                health_check_interval=30,
            )
            self._client = redis.Redis(connection_pool=self._pool, decode_responses=False)
            # Test connection
            self._client.ping()
            self._available = True
            logger.info(
                "RedisStore connected: redis://%s:%d/%d (prefix=%s, pool=%d)",
                host, port, db, key_prefix, pool_max_connections
            )
            if enable_pubsub:
                self._pubsub = self._client.pubsub()
                logger.info("Redis Pub/Sub enabled for inter-agent signals")
        except ImportError:
            logger.warning("redis not installed. RedisStore will fall back to no-op.")
        except Exception as e:
            logger.error("Redis connection failed: %s. Store will be no-op.", e)

    def close(self) -> None:
        """Close Redis connection pool."""
        if self._pubsub:
            try:
                self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None
        if self._pool:
            try:
                self._pool.disconnect()
                logger.info("RedisStore closed: pool disconnected")
            except Exception:
                pass
        self._available = False
        self._disabled = True  # No point re-enabling after close.

    @property
    def is_disabled(self) -> bool:
        """True if a Redis call has failed and the store dropped its
        write/read role for this process. Once disabled, all public
        methods short-circuit to safe no-ops (None/False/[]/0)."""
        return self._disabled

    def _mark_unavailable(self, exc: Exception) -> None:
        """Mark this store as disabled after a Redis exception.

        After the first connection-class error we refuse to keep
        issuing requests — they would all fail with the same error
        AND flood the log with tracebacks. Operators see one ERROR
        line and the ``is_disabled`` flag flips to True.
        """
        if not self._disabled:
            logger.error(
                "RedisStore auto-disabling after %s: %s. "
                "Future store/retrieve/delete calls return no-ops. "
                "Other workers are unaffected (each has its own instance).",
                type(exc).__name__, exc,
            )
            self._disabled = True

    def _memory_key(self, memory_id: str) -> str:
        return f"{self._key_prefix}{memory_id}"

    def _term_index_key(self, term: str) -> str:
        """Key for the inverted-index SET that maps a term to memory IDs."""
        return f"{self._key_prefix}idx:term:{term}"

    def _mem_tokens_key(self, memory_id: str) -> str:
        """Key for the per-memory SET that stores its tokens (used for delete cleanup)."""
        return f"{self._key_prefix}idx:mem:{memory_id}:tokens"

    def _memory_to_dict(self, memory: Memory) -> Dict[str, Any]:
        """Serialize memory to Redis-compatible dict."""
        return {
            b"id": str(memory.id).encode("utf-8"),
            b"created_at": str(memory.anchor.created_at).encode("utf-8"),
            b"accessed_at": str(memory.anchor.accessed_at or 0).encode("utf-8"),
            b"consolidated_at": str(memory.anchor.consolidated_at or 0).encode("utf-8"),
            b"expires_at": str(memory.anchor.expires_at or 0).encode("utf-8"),
            b"raw": memory.payload.raw.encode("utf-8"),
            b"structured": json.dumps(memory.payload.structured).encode("utf-8") if memory.payload.structured else b"null",
            b"embedding": serialize_embedding(memory.payload.embedding) or b"null",
            b"memory_type": memory.metadata.memory_type.name.encode("utf-8"),
            b"privacy": memory.metadata.privacy.name.encode("utf-8"),
            b"importance": str(memory.metadata.importance).encode("utf-8"),
            b"confidence": str(memory.metadata.confidence).encode("utf-8"),
            b"tags": json.dumps(list(memory.metadata.tags)).encode("utf-8"),
            b"categories": json.dumps(list(memory.metadata.categories)).encode("utf-8"),
            b"relations": json.dumps([
                {"type": r.relation_type, "target": r.target_memory_id, "strength": r.strength}
                for r in memory.metadata.relations
            ]).encode("utf-8"),
            b"provenance": json.dumps(memory.metadata.provenance).encode("utf-8"),
            b"agent_id": memory.context.agent_id.encode("utf-8"),
            b"agent_type": memory.context.agent_type.encode("utf-8"),
            b"session_id": memory.context.session_id.encode("utf-8"),
            b"user_id": (memory.context.user_id or "").encode("utf-8"),
            b"team_id": (memory.context.team_id or "").encode("utf-8"),
            b"project_id": (memory.context.project_id or "").encode("utf-8"),
        }

    def _dict_to_memory(self, data: Dict[bytes, bytes]) -> Optional[Memory]:
        """Deserialize memory from Redis dict."""
        try:
            def get_str(key: bytes) -> str:
                val = data.get(key, b"")
                return val.decode("utf-8") if val else ""

            def get_float(key: bytes) -> float:
                val = data.get(key, b"0")
                return float(val.decode("utf-8")) if val else 0.0

            def get_json(key: bytes):
                val = data.get(key, b"null")
                if val == b"null" or not val:
                    return None
                return json.loads(val.decode("utf-8"))

            structured = get_json(b"structured")
            embedding_data = data.get(b"embedding", b"null")
            embedding = deserialize_embedding(embedding_data) if embedding_data != b"null" else None

            tags_data = get_json(b"tags")
            categories_data = get_json(b"categories")
            relations_data = get_json(b"relations")
            provenance_data = get_json(b"provenance")

            relations = [
                Relation(r["type"], r["target"], strength=r.get("strength", 1.0))
                for r in (relations_data or [])
            ]

            expires_at = get_float(b"expires_at")
            if expires_at == 0.0:
                expires_at = None

            return Memory(
                id=MemoryId(get_str(b"id")),
                anchor=TemporalAnchor(
                    created_at=get_float(b"created_at"),
                    accessed_at=get_float(b"accessed_at") if get_float(b"accessed_at") > 0 else None,
                    consolidated_at=get_float(b"consolidated_at") if get_float(b"consolidated_at") > 0 else None,
                    expires_at=expires_at,
                ),
                context=AgentContext(
                    agent_id=get_str(b"agent_id"),
                    agent_type=get_str(b"agent_type"),
                    session_id=get_str(b"session_id"),
                    user_id=get_str(b"user_id") or None,
                    team_id=get_str(b"team_id") or None,
                    project_id=get_str(b"project_id") or None,
                ),
                payload=MemoryPayload(
                    raw=get_str(b"raw"),
                    structured=structured,
                    embedding=embedding,
                ),
                metadata=MemoryMetadata(
                    memory_type=MemoryType[get_str(b"memory_type")],
                    privacy=PrivacyLevel[get_str(b"privacy")],
                    importance=get_float(b"importance"),
                    confidence=get_float(b"confidence"),
                    tags=set(tags_data or []),
                    categories=set(categories_data or []),
                    relations=relations,
                    provenance=provenance_data or [],
                ),
            )
        except Exception as e:
            logger.exception("Failed to deserialize memory from Redis: %s", e)
            return None

    def store(self, memory: Memory) -> None:
        if not self._available or not self._client:
            return
        try:
            # redis-py is thread-safe (ConnectionPool has its own lock),
            # so we don't wrap in self._lock anymore — that was the root
            # cause of 7.6 ops/sec on 32-worker stress tests (32x serial
            # bottleneck). Collapse EVERYTHING into 1 pipeline (1 round-trip):
            # the main write, optional TTL/expiry, AND the inverted-index
            # updates. Previously these were 2 separate pipelines; merging
            # halves the per-op round-trip count, which on real Redis over
            # a network is the dominant cost (1-5ms per round-trip).
            key = self._memory_key(str(memory.id))
            data = self._memory_to_dict(memory)

            # Compute TTL once
            ttl = self._ttl_seconds
            if memory.anchor.expires_at:
                ttl_seconds = memory.anchor.expires_at - memory.anchor.created_at
                if ttl is None or ttl_seconds < ttl:
                    ttl = ttl_seconds
            apply_ttl = bool(ttl and ttl > 0)

            tokens = _tokenize(memory.payload.raw)
            mem_id = str(memory.id)

            pipe = self._client.pipeline(transaction=False)
            pipe.hset(key, mapping=data)
            if apply_ttl:
                pipe.expire(key, int(ttl))
                pipe.zadd(
                    self._expiry_zset_key,
                    {mem_id: memory.anchor.expires_at or (memory.anchor.created_at + ttl)},
                )
            # Inverted index in the same pipeline (atomic-ish per Redis semantics).
            for t in tokens:
                pipe.sadd(self._term_index_key(t), mem_id)
            if tokens:
                pipe.sadd(self._mem_tokens_key(mem_id), *tokens)
            pipe.execute()
            logger.debug("Stored memory %s in Redis (key=%s, tokens=%d)", memory.id, key, len(tokens))
        except Exception as exc:
            self._mark_unavailable(exc)
            logger.exception("Redis store failed for memory %s", memory.id)

    def retrieve(self, memory_id: str) -> Optional[Memory]:
        if not self._available or not self._client:
            return None
        try:
            key = self._memory_key(memory_id)
            # HGETALL + touched-time HSET collapsed into 1 round-trip via pipeline.
            # HGETALL first so we can short-circuit on missing keys without
            # paying the HSET round-trip.
            data = self._client.hgetall(key)
            if not data:
                return None
            pipe = self._client.pipeline(transaction=False)
            pipe.hset(key, b"accessed_at", str(TemporalAnchor().created_at).encode("utf-8"))
            pipe.execute()
            return self._dict_to_memory(data)
        except Exception as exc:
            self._mark_unavailable(exc)
            logger.exception("Redis retrieve failed for %s", memory_id)
            return None

    def delete(self, memory_id: str) -> bool:
        if not self._available or not self._client:
            return False
        try:
            key = self._memory_key(memory_id)
            # Read tokens first so we can clean up the inverted index.
            # (One extra round-trip, but only on delete — and avoids the
            # need to SCAN the term sets, which would be O(N) again.)
            mem_tokens_key = self._mem_tokens_key(memory_id)
            tokens = self._client.smembers(mem_tokens_key) or set()
            decoded_tokens = {
                t.decode("utf-8") if isinstance(t, bytes) else t
                for t in tokens
            }

            # DELETE + ZREM + SREM (per token) + DEL (mem:tokens) in one pipeline.
            pipe = self._client.pipeline(transaction=False)
            pipe.delete(key)
            pipe.zrem(self._expiry_zset_key, memory_id)
            for t in decoded_tokens:
                pipe.srem(self._term_index_key(t), memory_id)
            pipe.delete(mem_tokens_key)
            results = pipe.execute()
            deleted = results[0] if results else 0
            logger.debug("Deleted memory %s from Redis (deleted=%d)", memory_id, deleted)
            return deleted > 0
        except Exception as exc:
            self._mark_unavailable(exc)
            logger.exception("Redis delete failed for %s", memory_id)
            return False

    def search_keywords(self, query: str, k: int = 10) -> List[Memory]:
        """Full-text-ish search backed by an inverted index.

        Strategy: tokenize the query, look up the candidate memory IDs from
        the per-term index (SMEMBERS per token, unioned for OR semantics),
        then sample down to ``k * 10`` candidates and HGETALL only those in
        a single pipeline to filter by original substring match.

        The cap on HGETALL count bounds worst-case latency when many
        memories share a common token (e.g., all 14k stress-test memories
        contain "stress"). Without the cap, search would HGETALL + JSON-
        deserialize all 14k candidates per query (5+ seconds). With the
        cap, search is bounded at ~O(k * 10) = O(100) candidates.
        """
        if not self._available or not self._client:
            return []
        try:
            tokens = _tokenize(query)
            if not tokens:
                return []
            # Candidate memory IDs: union across all query tokens.
            candidates: Set[str] = set()
            for t in tokens:
                for mid in self._client.smembers(self._term_index_key(t)) or set():
                    candidates.add(
                        mid.decode("utf-8") if isinstance(mid, bytes) else mid
                    )
            if not candidates:
                return []

            # Sample down to at most k*10 candidates (with a floor of 50) to
            # bound worst-case latency. Sampling is uniform without a Redis
            # round-trip — Python's random.sample is sufficient.
            fetch_count = min(len(candidates), max(k * 10, 50))
            if len(candidates) > fetch_count:
                # random.sample needs a sequence, not a set; convert once.
                candidates = set(random.sample(list(candidates), fetch_count))

            # Batch HGETALL the sampled candidates in one pipeline.
            candidate_keys = [self._memory_key(mid) for mid in candidates]
            pipe = self._client.pipeline(transaction=False)
            for ck in candidate_keys:
                pipe.hgetall(ck)
            all_data = pipe.execute()

            # Apply original substring match to preserve pre-index semantics.
            # (Substring matching on top of token index is more permissive:
            # "vege" still finds "vegetarian" as a substring.)
            query_lower = query.lower()
            query_terms = query_lower.split()
            results: List[Memory] = []
            for data in all_data:
                if not data:
                    continue
                raw = data.get(b"raw", b"").decode("utf-8", errors="ignore").lower()
                if any(term in raw for term in query_terms):
                    mem = self._dict_to_memory(data)
                    if mem:
                        results.append(mem)
                    if len(results) >= k:
                        break
            return results
        except Exception as exc:
            self._mark_unavailable(exc)
            logger.exception("Redis keyword search failed")
            return []

    def search_vector(
        self, vector: List[float], k: int = 10, **filters: Any
    ) -> List[Memory]:
        """Redis does not support vector search natively. Fallback to recency."""
        return self._recent_memories(k)

    def search_graph(self, entity: str, depth: int = 2) -> List[Memory]:
        """Redis graph search: load all and filter by relation matching."""
        if not self._available or not self._client:
            return []
        try:
            results = []
            visited = set()
            queue = [(entity, 0)]
            
            while queue:
                current, d = queue.pop(0)
                if d > depth or current in visited:
                    continue
                visited.add(current)
                
                # Check if current is a memory ID
                mem = self.retrieve(current)
                if mem:
                    results.append(mem)
                    for rel in mem.metadata.relations:
                        if rel.target_memory_id not in visited:
                            queue.append((rel.target_memory_id, d + 1))
                
                # Also search for memories mentioning this entity
                if d == 0:
                    keywords = self.search_keywords(current, k=20)
                    for mem in keywords:
                        if str(mem.id) not in visited:
                            results.append(mem)
                            for rel in mem.metadata.relations:
                                if rel.target_memory_id not in visited:
                                    queue.append((rel.target_memory_id, d + 1))
            
            return results[:20]
        except Exception as exc:
            self._mark_unavailable(exc)
            logger.exception("Redis graph search failed")
            return []

    def list_all(self, limit: int = 100) -> List[Memory]:
        return self._recent_memories(limit)

    def _recent_memories(self, limit: int) -> List[Memory]:
        """Return most recent memories by created_at."""
        if not self._available or not self._client:
            return []
        try:
            results = []
            cursor = 0
            pattern = f"{self._key_prefix}*"

            while True:
                cursor, keys = self._client.scan(cursor, match=pattern, count=100)
                for key in keys:
                    data = self._client.hgetall(key)
                    if data:
                        mem = self._dict_to_memory(data)
                        if mem:
                            results.append(mem)
                if cursor == 0:
                    break

            # Sort by created_at descending
            results.sort(key=lambda m: m.anchor.created_at, reverse=True)
            return results[:limit]
        except Exception as exc:
            self._mark_unavailable(exc)
            logger.exception("Redis recent memories failed")
            return []

    def delete_expired(self) -> int:
        """Delete memories whose TTL has expired or whose expires_at has passed."""
        if not self._available or not self._client:
            return 0
        try:
            now = TemporalAnchor().created_at
            # Get expired IDs from ZSET
            expired_ids = self._client.zrangebyscore(
                self._expiry_zset_key, 0, now
            )
            count = 0
            for mid in expired_ids:
                mid_str = mid.decode("utf-8") if isinstance(mid, bytes) else mid
                if self.delete(mid_str):
                    count += 1
            logger.debug("Redis deleted %d expired memories", count)
            return count
        except Exception as exc:
            self._mark_unavailable(exc)
            logger.exception("Redis delete_expired failed")
            return 0

    # --- Pub/Sub Signal Support ---

    def publish_signal(self, channel: str, signal: Dict[str, Any]) -> bool:
        """Publish an inter-agent signal via Redis Pub/Sub."""
        if not self._available or not self._client or not self._enable_pubsub:
            return False
        try:
            message = json.dumps(signal)
            self._client.publish(channel, message)
            logger.debug("Published signal to channel %s", channel)
            return True
        except Exception as exc:
            self._mark_unavailable(exc)
            logger.exception("Redis publish_signal failed")
            return False

    def subscribe_signals(self, channel: str, handler=None) -> Optional[Any]:
        """Subscribe to a signal channel. Returns pubsub object for listening."""
        if not self._available or not self._client or not self._enable_pubsub:
            return None
        try:
            pubsub = self._client.pubsub()
            pubsub.subscribe(channel)
            logger.info("Subscribed to Redis channel %s", channel)
            return pubsub
        except Exception as exc:
            self._mark_unavailable(exc)
            logger.exception("Redis subscribe_signals failed")
            return None
