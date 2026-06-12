"""
Tile server — FastAPI application.

Endpoints:
  GET /health
  GET /tiles/{slide_id}/metadata
  GET /tiles/{slide_id}/thumbnail?width=256&height=256
  GET /tiles/{slide_id}/zxy/{z}/{x}/{y}

All tile and thumbnail responses carry long-lived Cache-Control headers so a
CDN or nginx proxy_cache can absorb the bulk of repeat requests.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

# Ensure app.* loggers emit to stderr alongside uvicorn's own loggers.
# uvicorn's dictConfig only configures uvicorn.* — root logger has no handler
# by default, so INFO from app.* would be silently dropped without this.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

from . import cache as tile_cache
from . import meta
from .annotations import init_db as init_annotation_db
from .annotations import router as annotation_router
from .config import settings
from .meta import get_patient_hierarchy, get_slide_dbmeta, search_suggestions
from .slides import SlideCache
from .tiles import get_thumbnail_bytes, get_tile_bytes, max_zoom, slide_metadata

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_slides: SlideCache | None = None
# In-process cache: image_id → s3 URI (populated on first open, survives across requests)
_path_cache: dict[str, str] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _slides
    _slides = SlideCache(capacity=settings.max_open_slides)
    await tile_cache.init_cache()
    if not settings.aws_endpoint_url:
        logger.warning(
            "AWS_ENDPOINT_URL is not set — S3 requests will go to public AWS "
            "(set this to your Dell ECS endpoint in production)"
        )
    logger.info(
        "Tile server ready. max_open_slides=%d endpoint=%s",
        settings.max_open_slides,
        settings.aws_endpoint_url or "AWS default",
    )
    await init_annotation_db()
    logger.info("Annotation DB ready: %s", settings.annotation_db_path)
    yield
    _slides.close_all()
    await tile_cache.close_cache()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="WSI Tile Server",
    description="Serve SVS whole-slide image tiles directly from Dell ECS (S3) via tiffslide.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

app.include_router(annotation_router)

TILE_CACHE_HEADERS  = {"Cache-Control": "public, max-age=604800, immutable"}  # 7 days — immutable image data
THUMB_CACHE_HEADERS = {"Cache-Control": "public, max-age=86400"}
# Patient/sample metadata contains PHI — must not be cached by shared/public proxies
PHI_CACHE_HEADERS   = {"Cache-Control": "private, no-store"}


async def _in_thread(fn, *args):
    """Run a blocking function in the default thread-pool executor."""
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_slide_id(image_id: str) -> str:
    """Resolve an image_id to its S3 URI, with in-process caching."""
    if image_id in _path_cache:
        return _path_cache[image_id]
    path = meta.get_slide_path(image_id, settings.databricks_warehouse_id)
    if not path:
        raise FileNotFoundError(f"Slide not found: {image_id}")
    _path_cache[image_id] = path
    return path


def _get_slide(image_id: str):
    """Resolve image_id → S3 path, open/retrieve from cache; raise 404 on failure."""
    try:
        s3_uri = _resolve_slide_id(image_id)
        return _slides.get(s3_uri)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Slide not found: {image_id}")
    except Exception:
        logger.exception("Failed to open slide %s", image_id)
        raise HTTPException(status_code=500, detail="Failed to open slide")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "n_workers": settings.n_workers}


# ---------------------------------------------------------------------------
# Databricks metadata routes
# ---------------------------------------------------------------------------

@app.get("/patient/{patient_id}")
async def patient_hierarchy(patient_id: str):
    """
    Return the full patient slide hierarchy sourced from Databricks
    (DEID_TABLE joined with INVENTORY_TABLE — see app/constants.py).

    Structure: { patient_id, samples: [{ sample_id, cancer_type, …,
      parts: [{ part_number, …, blocks: [{ block_number, …,
        slides: [{ image_id, stain_name, can_serve_tiles, … }] }] }] }] }
    """
    cached = await tile_cache.get_patient(patient_id)
    if cached is not None:
        return Response(content=json.dumps(cached), media_type="application/json",
                        headers=PHI_CACHE_HEADERS)

    try:
        result = await _in_thread(
            get_patient_hierarchy,
            patient_id,
            settings.databricks_warehouse_id,
        )
    except Exception:
        logger.exception("Databricks query failed for patient %s", patient_id)
        raise HTTPException(status_code=502, detail="Metadata query failed")

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Patient not found in slide inventory",
        )

    await tile_cache.set_patient(patient_id, result)
    return Response(content=json.dumps(result), media_type="application/json",
                    headers=PHI_CACHE_HEADERS)


@app.get("/slides/{image_id}/dbmeta")
async def slide_dbmeta(image_id: str):
    """Return the raw Databricks metadata row for a single slide (by numeric image_id)."""
    try:
        result = await _in_thread(
            get_slide_dbmeta,
            image_id,
            settings.databricks_warehouse_id,
        )
    except Exception:
        logger.exception("Databricks query failed for slide %s", image_id)
        raise HTTPException(status_code=502, detail="Metadata query failed")

    if result is None:
        raise HTTPException(status_code=404, detail="Slide not found")
    return Response(content=json.dumps(result, default=str),
                    media_type="application/json", headers=PHI_CACHE_HEADERS)


@app.get("/search")
async def search(q: str = ""):
    """
    Autocomplete suggestions for the search bar.

    Returns up to 8 items: [{ type, id, label, sublabel }]
    Detects query pattern: P-xxx → patients, P-xxx-Tx → samples, digits → slides.
    Results are cached in Redis for 5 minutes.
    """
    q = q.strip()
    if len(q) < 2:
        return []

    cache_key = f"search:{q.lower()}"
    cached = await tile_cache.get_raw(cache_key)
    if cached is not None:
        return Response(content=json.dumps(cached, default=str),
                        media_type="application/json", headers=PHI_CACHE_HEADERS)

    try:
        results = await _in_thread(
            search_suggestions,
            q,
            settings.databricks_warehouse_id,
        )
    except Exception:
        logger.exception("Search query failed for %r", q)
        raise HTTPException(status_code=502, detail="Search query failed")

    await tile_cache.set_raw(cache_key, results, ttl=300)
    return Response(content=json.dumps(results, default=str),
                    media_type="application/json", headers=PHI_CACHE_HEADERS)


@app.get("/tiles/{slide_id}/metadata")
async def metadata(slide_id: str):
    cached = await tile_cache.get_metadata(slide_id)
    if cached is not None:
        return cached
    slide = await _in_thread(_get_slide, slide_id)
    result = await _in_thread(slide_metadata, slide)
    await tile_cache.set_metadata(slide_id, result)
    return result


@app.get("/tiles/{slide_id}/warmup", include_in_schema=False)
def warmup(slide_id: str):
    """Fetch and discard the overview tile to prime the TiffSlide cache on this worker."""
    try:
        slide = _slides.get(slide_id)
        get_tile_bytes(slide, 0, 0, 0)
    except Exception:
        pass
    return {"status": "ok"}


@app.get("/tiles/{slide_id}/thumbnail")
async def thumbnail(
    slide_id: str,
    width: int = 256,
    height: int = 256,
):
    width = max(1, min(width, 2048))
    height = max(1, min(height, 2048))

    cached = await tile_cache.get_thumbnail(slide_id, width, height)
    if cached:
        return Response(content=cached, media_type="image/jpeg",
                        headers=THUMB_CACHE_HEADERS)

    slide = await _in_thread(_get_slide, slide_id)
    data = await _in_thread(get_thumbnail_bytes, slide, width, height)

    await tile_cache.set_thumbnail(slide_id, width, height, data)
    return Response(content=data, media_type="image/jpeg",
                    headers=THUMB_CACHE_HEADERS)


@app.get("/tiles/{slide_id}/zxy/{z}/{x}/{y}")
async def tile(slide_id: str, z: int, x: int, y: int):
    cached = await tile_cache.get_tile(slide_id, z, x, y)
    if cached:
        return Response(content=cached, media_type="image/jpeg",
                        headers=TILE_CACHE_HEADERS)

    slide = await _in_thread(_get_slide, slide_id)

    try:
        data = await _in_thread(get_tile_bytes, slide, z, x, y)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("Tile extraction failed for %s z=%d x=%d y=%d",
                         slide_id, z, x, y)
        raise HTTPException(status_code=500, detail=str(exc))

    await tile_cache.set_tile(slide_id, z, x, y, data)
    return Response(content=data, media_type="image/jpeg",
                    headers=TILE_CACHE_HEADERS)
