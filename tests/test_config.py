"""Tests for app/config.py — verifies correct env var names are read.

These tests exist specifically to catch regressions like the
ECS_ENDPOINT_URL vs AWS_ENDPOINT_URL mismatch that silently broke
all S3 tile requests in production.
"""

import os
from unittest.mock import patch

import pytest

from app.config import Settings


def make_settings(**env):
    """Create a fresh Settings() with only the given env vars active."""
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("AWS_", "DATABRICKS_", "REDIS", "TILE_",
                                   "JPEG_", "MAX_", "N_WORKERS", "BLOCKCACHE",
                                   "CORS_", "PATIENT_", "ANNOTATION_", "KEYCLOAK_"))}
    clean.update(env)
    with patch.dict(os.environ, clean, clear=True):
        with patch("app.config._aws_profile", return_value=""):
            return Settings()


class TestS3EnvVars:
    def test_endpoint_reads_AWS_ENDPOINT_URL(self):
        s = make_settings(AWS_ENDPOINT_URL="http://ecs.example.com:9020")
        assert s.aws_endpoint_url == "http://ecs.example.com:9020"

    def test_endpoint_not_read_from_ECS_ENDPOINT_URL(self):
        """Regression: old name ECS_ENDPOINT_URL must NOT be used."""
        s = make_settings(ECS_ENDPOINT_URL="http://ecs.example.com:9020")
        assert s.aws_endpoint_url == ""

    def test_access_key_reads_AWS_ACCESS_KEY_ID(self):
        s = make_settings(AWS_ACCESS_KEY_ID="AKIATEST")
        assert s.aws_access_key_id == "AKIATEST"

    def test_secret_reads_AWS_SECRET_ACCESS_KEY(self):
        s = make_settings(AWS_SECRET_ACCESS_KEY="supersecret")
        assert s.aws_secret_access_key == "supersecret"

    def test_endpoint_empty_when_unset(self):
        s = make_settings()
        assert s.aws_endpoint_url == ""

    def test_env_takes_priority_over_profile(self):
        with patch("app.config._aws_profile", return_value="http://from-profile:9020"):
            with patch.dict(os.environ, {"AWS_ENDPOINT_URL": "http://from-env:9020"}):
                s = Settings()
        assert s.aws_endpoint_url == "http://from-env:9020"

    def test_profile_used_when_env_absent(self):
        clean = {k: v for k, v in os.environ.items() if k != "AWS_ENDPOINT_URL"}
        with patch.dict(os.environ, clean, clear=True):
            with patch("app.config._aws_profile", return_value="http://from-profile:9020"):
                s = Settings()
        assert s.aws_endpoint_url == "http://from-profile:9020"


class TestOtherSettings:
    def test_databricks_warehouse_id_from_env(self):
        s = make_settings(DATABRICKS_WAREHOUSE_ID="wh-test-123")
        assert s.databricks_warehouse_id == "wh-test-123"

    def test_annotation_database_url_from_env(self):
        s = make_settings(ANNOTATION_DATABASE_URL="postgresql://user:pass@host/db")
        assert s.annotation_database_url == "postgresql://user:pass@host/db"

    def test_cors_origins_parsed(self):
        s = make_settings(CORS_ORIGINS="https://a.example.com,https://b.example.com")
        assert s.cors_origins == ["https://a.example.com", "https://b.example.com"]

    def test_cors_origins_strips_whitespace(self):
        s = make_settings(CORS_ORIGINS="https://a.example.com, https://b.example.com")
        assert s.cors_origins == ["https://a.example.com", "https://b.example.com"]
