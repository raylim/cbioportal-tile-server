import configparser
import os

from .constants import DEFAULT_WAREHOUSE_ID as _DEFAULT_WAREHOUSE_ID


def _aws_profile(key: str, fallback: str = "") -> str:
    """Read a value from the [ecs] section of ~/.aws/credentials, if present."""
    try:
        cfg = configparser.ConfigParser()
        cfg.read(os.path.expanduser("~/.aws/credentials"))
        return cfg.get("ecs", key, fallback=fallback)
    except Exception:
        return fallback


class Settings:
    def __init__(self):
        # ── S3 / Dell ECS connection ─────────────────────────────────────────
        # Uses canonical AWS SDK env var names.  Slide paths are stored as full
        # s3:// URIs in the Databricks inventory table — no bucket/prefix config needed.
        self.aws_endpoint_url: str = os.environ.get(
            "AWS_ENDPOINT_URL", _aws_profile("endpoint_url", "")
        )
        self.aws_access_key_id: str = os.environ.get(
            "AWS_ACCESS_KEY_ID", _aws_profile("aws_access_key_id")
        )
        self.aws_secret_access_key: str = os.environ.get(
            "AWS_SECRET_ACCESS_KEY", _aws_profile("aws_secret_access_key")
        )

        # Tile settings
        self.tile_size: int = int(os.environ.get("TILE_SIZE", "256"))
        self.jpeg_quality: int = int(os.environ.get("JPEG_QUALITY", "85"))

        # Redis tile cache
        self.redis_url: str = os.environ.get("REDIS_URL", "redis://redis:6379")
        # Tile bytes are immutable — cache forever (0 = no expiry)
        self.tile_cache_ttl: int = int(os.environ.get("TILE_CACHE_TTL", "0"))

        # How many TiffSlide objects to keep open simultaneously.
        # Each open SVS consumes ~50–200 MB depending on the pyramid headers.
        self.max_open_slides: int = int(os.environ.get("MAX_OPEN_SLIDES", "256"))

        # Number of gunicorn workers — used to fire warmup that many times so
        # every worker's SlideCache gets primed.  Match the --workers flag in CMD.
        self.n_workers: int = int(os.environ.get("N_WORKERS", "4"))

        # Databricks SQL — for patient/slide metadata lookups.
        # DATABRICKS_HOST and DATABRICKS_TOKEN are auto-detected from the environment
        # by databricks-sdk (env vars) or from ~/.databrickscfg; only the warehouse
        # ID needs to be explicit here.
        self.databricks_warehouse_id: str = os.environ.get(
            "DATABRICKS_WAREHOUSE_ID", _DEFAULT_WAREHOUSE_ID
        )

        # Patient hierarchy cache TTL (seconds).  Cached in Redis under
        # patient:{patient_id}.  Set to 0 to disable.  Default = 24 h.
        self.patient_cache_ttl: int = int(os.environ.get("PATIENT_CACHE_TTL", str(86_400)))

        # fsspec BlockCache — set BLOCKCACHE_PATH to enable (e.g. /nvme/slide-cache).
        # Blocks are written to disk and survive process restarts (persistent warm-up).
        # BLOCKCACHE_BLOCK_SIZE: bytes per cache block.  8 MB amortises ECS round-trips
        # well for SVS which stores tile data in large contiguous strips.
        self.blockcache_path: str = os.environ.get("BLOCKCACHE_PATH", "")
        self.blockcache_block_size: int = int(
            os.environ.get("BLOCKCACHE_BLOCK_SIZE", str(8 * 1024 * 1024))  # 8 MB
        )

        # ── Annotation API ───────────────────────────────────────────────────
        # Path on disk where the SQLite annotation database lives.
        # Persist this via a named Docker volume.
        self.annotation_db_path: str = os.environ.get(
            "ANNOTATION_DB_PATH", "/data/annotations.db"
        )

        # Keycloak JWKS endpoint used to validate annotation API JWTs.
        # Example: https://keycloak.mskcc.org/realms/msk/protocol/openid-connect/certs
        self.keycloak_jwks_url: str = os.environ.get("KEYCLOAK_JWKS_URL", "")

        # Set ANNOTATION_AUTH_ENABLED=false to skip JWT validation in dev/CI.
        self.annotation_auth_enabled: bool = (
            os.environ.get("ANNOTATION_AUTH_ENABLED", "true").lower() != "false"
        )

        # CORS — comma-separated list of allowed origins.
        # Default restricts to known public cBioPortal origins; override via env
        # to add internal hostnames (e.g. https://cbioportal.mskcc.org).
        self.cors_origins: list = [
            o.strip()
            for o in os.environ.get(
                "CORS_ORIGINS",
                "https://cbioportal.org,https://www.cbioportal.org,https://genie.cbioportal.org",
            ).split(",")
            if o.strip()
        ]


settings = Settings()
