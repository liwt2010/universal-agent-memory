"""Health check and metrics server for UAMS.

Provides /health and /ready endpoints for Kubernetes/Compose.
Exposes Prometheus-compatible metrics on /metrics.
"""

from __future__ import annotations

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from uams.utils.logging import get_logger

logger = get_logger(__name__)


class MetricsCollector:
    """Thread-safe metrics collector with ring buffer to prevent memory leaks."""

    def __init__(self, max_histogram_entries: int = 10000):
        self._counters: dict[str, int] = {}
        self._histograms: dict[str, list] = {}
        self._histogram_stats: dict[str, dict] = {}  # aggregated stats after ring buffer overflow
        self._max_histogram_entries = max_histogram_entries
        self._lock = threading.Lock()

    def inc(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = []
            self._histograms[name].append(value)
            # Ring buffer: when overflow, aggregate to stats and reset
            if len(self._histograms[name]) > self._max_histogram_entries:
                values = self._histograms[name]
                existing = self._histogram_stats.get(name, {})
                self._histogram_stats[name] = {
                    "count": len(values) + existing.get("count", 0),
                    "sum": sum(values) + existing.get("sum", 0),
                    "min": min(values) if not existing else min(min(values), existing.get("min", float("inf"))),
                    "max": max(values) if not existing else max(max(values), existing.get("max", float("-inf"))),
                }
                self._histograms[name] = []
                logger.debug("Metrics histogram %s aggregated (batch_count=%d)", name, len(values))

    def render(self) -> str:
        with self._lock:
            lines = []
            for name, value in self._counters.items():
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{name} {value}")
            for name, values in self._histograms.items():
                if values or name in self._histogram_stats:
                    agg = self._histogram_stats.get(name, {})
                    total_count = len(values) + agg.get("count", 0)
                    total_sum = sum(values) + agg.get("sum", 0)
                    lines.append(f"# TYPE {name} histogram")
                    lines.append(f"{name}_count {total_count}")
                    lines.append(f"{name}_sum {total_sum}")
                    if total_count > 0:
                        lines.append(f"{name}_avg {total_sum/total_count:.4f}")
                    if agg:
                        lines.append(f"{name}_min {agg['min']}")
                        lines.append(f"{name}_max {agg['max']}")
            return "\n".join(lines) + "\n"


class HealthHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for health and metrics."""

    metrics: Optional[MetricsCollector] = None
    uams_instance = None

    def log_message(self, format, *args):
        logger.debug(format, *args)

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, "ok", "text/plain")
        elif self.path == "/ready":
            ready = self._check_ready()
            status = 200 if ready else 503
            body = "ready" if ready else "not_ready"
            self._respond(status, body, "text/plain")
        elif self.path == "/metrics":
            if self.metrics:
                self._respond(200, self.metrics.render(), "text/plain")
            else:
                self._respond(200, "", "text/plain")
        elif self.path == "/stats":
            stats = self._get_stats()
            self._respond(200, stats, "text/plain")
        else:
            self._respond(404, "not found", "text/plain")

    def _respond(self, code: int, body: str, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _check_ready(self) -> bool:
        if self.uams_instance is None:
            return False
        try:
            stats = self.uams_instance.get_stats()
            return isinstance(stats, dict)
        except Exception:
            return False

    def _get_stats(self) -> str:
        if self.uams_instance is None:
            return "no uams instance"
        try:
            stats = self.uams_instance.get_stats()
            lines = [f"{k}: {v}" for k, v in stats.items()]
            return "\n".join(lines)
        except Exception as e:
            return f"error: {e}"


class HealthServer:
    """Background HTTP server for health checks and metrics."""

    def __init__(
        self,
        port: int = 3111,
        metrics: MetricsCollector | None = None,
        histogram_max_entries: int = 10000,
    ):
        self._port = port
        # Use the explicit metrics collector if the caller built one
        # (e.g. with a custom histogram cap from UAMSConfig); otherwise
        # build one honouring the passed-in cap so callers without a
        # pre-built metrics object can still tune ring-buffer size.
        self._metrics = metrics or MetricsCollector(
            max_histogram_entries=histogram_max_entries,
        )
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def metrics(self) -> MetricsCollector:
        return self._metrics

    def start(self, uams_instance=None) -> None:
        HealthHandler.metrics = self._metrics
        HealthHandler.uams_instance = uams_instance
        self._server = HTTPServer(("0.0.0.0", self._port), HealthHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("Health server started on http://0.0.0.0:%d", self._port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            logger.info("Health server stopped")
