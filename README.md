# cbioportal-tile-server

FastAPI tile server that streams SVS whole-slide images from Dell ECS (S3-compatible) to
OpenSeadragon via ZXY tile requests.  Used as the backend for the cBioPortal H&E slide viewer.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/patient/{patient_id}` | Slide hierarchy from Databricks |
| GET | `/slides/{image_id}/dbmeta` | Raw Databricks row for a slide |
| GET | `/search?q=` | Autocomplete suggestions |
| GET | `/tiles/{slide_id}/metadata` | Slide dimensions, zoom levels, MPP |
| GET | `/tiles/{slide_id}/thumbnail` | JPEG thumbnail |
| GET | `/tiles/{slide_id}/zxy/{z}/{x}/{y}` | ZXY tile (JPEG) |

## Quick start

```bash
python3 tools/write_dev_env.py > .env   # populate from ~/.aws/credentials + ~/.databrickscfg
docker compose up --build
```

## Configuration

All settings are environment variables (see `app/config.py`):

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ENDPOINT_URL` | — | Dell ECS endpoint |
| `AWS_ACCESS_KEY_ID` | — | ECS access key |
| `AWS_SECRET_ACCESS_KEY` | — | ECS secret key |
| `DATABRICKS_HOST` | — | Databricks workspace URL |
| `DATABRICKS_TOKEN` | — | Databricks PAT |
| `DATABRICKS_WAREHOUSE_ID` | `0b49b7d78734ad5c` | SQL warehouse |
| `ANNOTATION_DATABASE_URL` | — | Optional Postgres/Lakebase DSN for annotation storage |
| `ANNOTATION_DB_PATH` | `/data/annotations.db` | SQLite path used when `ANNOTATION_DATABASE_URL` is unset |
| `REDIS_URL` | `redis://redis:6379` | Redis connection |
| `MAX_OPEN_SLIDES` | `256` | LRU slide cache capacity |
| `BLOCKCACHE_PATH` | — | NVMe block cache directory |
| `CORS_ORIGINS` | public cBioPortal origins | Comma-separated allowed origins |

## Running tests

```bash
uv run pytest
```

## Databricks bundles

- Root [databricks.yml](databricks.yml) manages the nightly WSI summary pipeline job.
- [databricks/lakebase/README.md](databricks/lakebase/README.md) documents the
  separate Lakebase bundle used to provision annotation storage.
