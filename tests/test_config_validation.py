"""Tests for production-grade config validation.

Covers the strictness ladder:
- default UAMSConfig() must always validate (backward-compat)
- structural bounds for every new field
- environment-aware checks:
  - development: only structural, no warnings
  - staging: structural + warnings (still passes)
  - production: structural + rejects insecure defaults
"""

import os
import sys
import unittest
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from uams.config import UAMSConfig


class TestUAMSConfigDefaults(unittest.TestCase):
    """The default config must always validate (backward compat)."""

    def test_default_config_passes(self):
        UAMSConfig().validate()

    def test_default_environment_is_development(self):
        self.assertEqual(UAMSConfig().environment, "development")


class TestUAMSConfigStructuralBounds(unittest.TestCase):
    """Every new field must have a bound enforced."""

    def test_invalid_environment(self):
        with self.assertRaises(ValueError) as ctx:
            UAMSConfig(environment="qa").validate()
        self.assertIn("environment", str(ctx.exception))

    def test_invalid_privacy_level(self):
        with self.assertRaises(ValueError) as ctx:
            UAMSConfig(default_privacy_level="top-secret").validate()
        self.assertIn("default_privacy_level", str(ctx.exception))

    def test_privacy_level_case_insensitive(self):
        # lower/upper should both pass
        UAMSConfig(default_privacy_level="PUBLIC").validate()
        UAMSConfig(default_privacy_level="Internal").validate()

    def test_session_events_bounds(self):
        with self.assertRaises(ValueError):
            UAMSConfig(max_session_events=0).validate()

    def test_half_life_below_minimum(self):
        with self.assertRaises(ValueError) as ctx:
            UAMSConfig(working_ttl_seconds=30).validate()  # < 60s
        self.assertIn("working_ttl_seconds", str(ctx.exception))

    def test_half_life_above_maximum(self):
        # 11 years > 10 year max
        with self.assertRaises(ValueError):
            UAMSConfig(episodic_half_life_seconds=11 * 365 * 24 * 3600).validate()

    def test_all_half_life_fields_have_bounds(self):
        fields = (
            "working_ttl_seconds",
            "episodic_half_life_seconds",
            "semantic_half_life_seconds",
            "procedural_half_life_seconds",
        )
        for f in fields:
            with self.subTest(field=f):
                with self.assertRaises(ValueError):
                    UAMSConfig(**{f: 1}).validate()  # 1s < 60s min

    def test_connection_timeout_bounds(self):
        with self.assertRaises(ValueError):
            UAMSConfig(connection_timeout_seconds=0.05).validate()
        with self.assertRaises(ValueError):
            UAMSConfig(connection_timeout_seconds=120).validate()

    def test_read_timeout_bounds(self):
        with self.assertRaises(ValueError):
            UAMSConfig(read_timeout_seconds=0.5).validate()
        with self.assertRaises(ValueError):
            UAMSConfig(read_timeout_seconds=1000).validate()

    def test_agent_id_length_bounds(self):
        with self.assertRaises(ValueError):
            UAMSConfig(max_agent_id_length=0).validate()
        with self.assertRaises(ValueError):
            UAMSConfig(max_agent_id_length=99999).validate()


class TestUAMSConfigCoupling(unittest.TestCase):
    """Coupling rules: field A implies field B."""

    def test_sqlite_requires_path(self):
        with self.assertRaises(ValueError):
            UAMSConfig(storage_backend="sqlite", sqlite_path="").validate()

    def test_audit_log_requires_path(self):
        with self.assertRaises(ValueError) as ctx:
            UAMSConfig(enable_audit_log=True, audit_log_path=None).validate()
        self.assertIn("audit_log_path", str(ctx.exception))

    def test_audit_log_with_path_passes(self):
        UAMSConfig(enable_audit_log=True, audit_log_path="/var/log/uams.log").validate()


class TestUAMSConfigProductionSafety(unittest.TestCase):
    """Production must refuse insecure defaults; staging must warn."""

    def test_development_with_default_neo4j_password_passes(self):
        # development: default creds OK
        UAMSConfig(environment="development", storage_backend="neo4j").validate()

    def test_production_neo4j_default_password_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            UAMSConfig(
                environment="production",
                storage_backend="neo4j",
                neo4j_password="password",
                neo4j_use_tls=True,
            ).validate()
        self.assertIn("neo4j_password", str(ctx.exception))

    def test_production_neo4j_plain_uri_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            UAMSConfig(
                environment="production",
                storage_backend="neo4j",
                neo4j_password="real-secret",
                neo4j_use_tls=False,
                neo4j_uri="bolt://prod-host:7687",
            ).validate()
        self.assertIn("neo4j", str(ctx.exception).lower())

    def test_production_postgresql_default_password_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            UAMSConfig(
                environment="production",
                storage_backend="postgresql",
                postgresql_user="uams",
                postgresql_password="uams",
                postgresql_use_tls=True,
            ).validate()
        self.assertIn("postgresql", str(ctx.exception))

    def test_production_postgresql_plain_connection_rejected(self):
        with self.assertRaises(ValueError):
            UAMSConfig(
                environment="production",
                storage_backend="postgresql",
                postgresql_user="real_svc",
                postgresql_password="real-secret",
                postgresql_use_tls=False,
            ).validate()

    def test_production_redis_empty_password_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            UAMSConfig(
                environment="production",
                storage_backend="redis",
                redis_password=None,
                redis_use_tls=True,
            ).validate()
        self.assertIn("redis_password", str(ctx.exception))

    def test_production_redis_plain_rejected(self):
        with self.assertRaises(ValueError):
            UAMSConfig(
                environment="production",
                storage_backend="redis",
                redis_password="real-secret",
                redis_use_tls=False,
            ).validate()

    def test_production_full_happy_path(self):
        # All required prod-safety knobs set; should validate cleanly.
        cfg = UAMSConfig(
            environment="production",
            storage_backend="postgresql",
            postgresql_user="uams_svc",
            postgresql_password="real-secret-123",
            postgresql_use_tls=True,
        )
        cfg.validate()

    def test_staging_warns_but_passes(self):
        # staging should log warnings but NOT raise
        with self.assertLogs("uams.config", level="WARNING") as cm:
            cfg = UAMSConfig(
                environment="staging",
                storage_backend="neo4j",
                neo4j_password="password",
                neo4j_use_tls=False,
            )
            cfg.validate()
        self.assertTrue(any("neo4j" in m for m in cm.output))


class TestUAMSConfigIdentifierSafety(unittest.TestCase):
    """postgresql_table and redis_key_prefix are interpolated into raw DDL /
    keyspace strings, so they must be safe identifiers — never user input.

    Pre-fix, an attacker who could set UAMS_POSTGRESQL_TABLE (e.g. via env
    variable injection in a multi-tenant control plane) could smuggle a
    DROP TABLE into the schema migration. These tests pin the safe-
    identifier check."""

    def _try_validate(self, **overrides) -> "UAMSConfig":
        cfg = UAMSConfig(**overrides)
        try:
            cfg.validate()
        except ValueError:
            return None  # raised; caller inspects
        return cfg

    def test_postgresql_table_with_ddl_injection_rejected(self):
        cfg = self._try_validate(postgresql_table="uams; DROP TABLE x; --")
        assert cfg is None, "postgresql_table with SQL injection must reject"

    def test_postgresql_table_with_spaces_rejected(self):
        cfg = self._try_validate(postgresql_table="my table")
        assert cfg is None

    def test_postgresql_table_with_dot_rejected(self):
        cfg = self._try_validate(postgresql_table="schema.table")
        assert cfg is None

    def test_postgresql_table_valid_id_accepted(self):
        cfg = self._try_validate(postgresql_table="uams_memories_abc123")
        assert cfg is not None, "valid identifier should pass"

    def test_redis_key_prefix_with_shell_meta_rejected(self):
        cfg = self._try_validate(redis_key_prefix="uams:|; rm -rf /")
        assert cfg is None


class TestUAMSConfigAuditLogPathSafety(unittest.TestCase):
    """Audit log paths reach open() — reject shell-meta characters."""

    def test_path_with_semicolon_rejected(self):
        cfg = UAMSConfig(cascade_audit_log_path="logs/x; touch /tmp/pwn")
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_path_with_pipe_rejected(self):
        cfg = UAMSConfig(cascade_audit_log_path="logs/x | cat /etc/passwd")
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_path_with_nul_rejected(self):
        cfg = UAMSConfig(cascade_audit_log_path="logs/x\x00.log")
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_normal_path_passes(self):
        UAMSConfig(cascade_audit_log_path="logs/cascade_forget_audit.jsonl").validate()


class TestUAMSConfigCascadeBounds(unittest.TestCase):
    """cascade_max_depth is bounded to prevent accidental deep-graph traversals."""

    def test_depth_zero_accepted(self):
        UAMSConfig(cascade_max_depth=0).validate()

    def test_depth_eight_accepted(self):
        UAMSConfig(cascade_max_depth=8).validate()

    def test_depth_nine_rejected(self):
        with self.assertRaises(ValueError):
            UAMSConfig(cascade_max_depth=9).validate()

    def test_depth_negative_rejected(self):
        with self.assertRaises(ValueError):
            UAMSConfig(cascade_max_depth=-1).validate()


class TestUAMSConfigFromEnv(unittest.TestCase):
    """from_env() reads the new env vars correctly."""

    def test_environment_from_env(self):
        os.environ["UAMS_ENVIRONMENT"] = "production"
        try:
            cfg = UAMSConfig.from_env()
            self.assertEqual(cfg.environment, "production")
        finally:
            del os.environ["UAMS_ENVIRONMENT"]

    def test_tls_from_env(self):
        os.environ["UAMS_REDIS_TLS"] = "true"
        os.environ["UAMS_NEO4J_TLS"] = "TRUE"
        os.environ["UAMS_PG_TLS"] = "1"
        try:
            cfg = UAMSConfig.from_env()
            self.assertTrue(cfg.redis_use_tls)
            self.assertTrue(cfg.neo4j_use_tls)
            self.assertTrue(cfg.postgresql_use_tls)
        finally:
            for k in ("UAMS_REDIS_TLS", "UAMS_NEO4J_TLS", "UAMS_PG_TLS"):
                os.environ.pop(k, None)

    def test_invalid_int_env_raises(self):
        os.environ["UAMS_EVENT_BUS_MAX_BUFFER"] = "not-a-number"
        try:
            with self.assertRaises(ValueError):
                UAMSConfig.from_env()
        finally:
            del os.environ["UAMS_EVENT_BUS_MAX_BUFFER"]


if __name__ == "__main__":
    unittest.main()