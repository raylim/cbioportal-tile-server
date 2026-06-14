"""
Databricks metadata layer — patient / slide hierarchy.

Table names are defined in app/constants.py (DEID_TABLE, INVENTORY_TABLE).
"""

from __future__ import annotations

import logging
from typing import Any

from .constants import DEID_TABLE as _TABLE, INVENTORY_TABLE as _INVENTORY, SUMMARY_TABLE as _SUMMARY

logger = logging.getLogger(__name__)

_PATIENT_SQL = f"""
SELECT
    d.image_id, d.PATIENT_ID_IMPACT, d.SAMPLE_ID_IMPACT, d.SAMPLE_ID_PATH,
    d.PART_NUMBER, d.part_designator, d.part_type, d.part_description,
    d.BLOCK_NUMBER, d.BLOCK_LABEL, d.barcode,
    d.IS_HNE, d.IS_IHC, d.stain_name, d.stain_group,
    d.subspecialty, d.CANCER_TYPE, d.CANCER_TYPE_DETAILED, d.ONCOTREE_CODE,
    d.PRIMARY_SITE, d.SAMPLE_TYPE, d.METASTATIC_SITE, d.TUMOR_PURITY,
    d.ONCOGENIC_MUTATIONS, d.`#ONCOGENIC_MUTATIONS` AS NUM_ONCOGENIC_MUTATIONS,
    d.CVR_TMB_SCORE, d.MSI_TYPE, d.magnification,
    d.file_size_bytes,
    d.PATH_DX_SPEC_TITLE, d.PATH_DX_SPEC_DESC,
    s.path AS slide_path
FROM {_TABLE} d
LEFT JOIN {_INVENTORY} s ON d.image_id = s.image_id
WHERE d.PATIENT_ID_IMPACT = :patient_id
ORDER BY d.SAMPLE_ID_IMPACT, d.PART_NUMBER, d.BLOCK_NUMBER, d.image_id
"""

_SLIDE_SQL = f"""
SELECT *
FROM {_TABLE}
WHERE image_id = :image_id
LIMIT 1
"""

_SLIDE_PATH_SQL = f"""
SELECT path FROM {_INVENTORY}
WHERE image_id = :image_id
LIMIT 1
"""


def _param(name: str, value: str, ptype: str = "STRING"):
    """Build a Databricks StatementParameterListItem (lazy import avoids load-time SDK dependency)."""
    from databricks.sdk.service.sql import StatementParameterListItem  # noqa: PLC0415
    return StatementParameterListItem(name=name, value=value, type=ptype)


def _client():
    """Lazy singleton WorkspaceClient — picks up env-var credentials automatically."""
    from databricks.sdk import WorkspaceClient  # noqa: PLC0415
    if not hasattr(_client, "_inst"):
        _client._inst = WorkspaceClient()  # type: ignore[attr-defined]
    return _client._inst  # type: ignore[attr-defined]


def _run_query(sql: str, warehouse_id: str,
               params: list | None = None) -> list[dict[str, Any]]:
    import time  # noqa: PLC0415

    from databricks.sdk.service.sql import (  # noqa: PLC0415
        StatementState,
    )

    stmt = _client().statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        parameters=params or [],
        wait_timeout="50s",
    )

    # If Databricks didn't finish within the inline 50 s window, poll.
    # This runs inside _in_thread() so blocking sleep is safe.
    _POLL_INTERVAL = 2   # seconds between polls
    _MAX_POLL     = 120  # give up after this many extra seconds
    elapsed = 0
    while stmt.status.state in (StatementState.RUNNING, StatementState.PENDING):
        if elapsed >= _MAX_POLL:
            try:
                _client().statement_execution.cancel_execution(stmt.statement_id)
            except Exception:
                pass
            raise RuntimeError("Databricks query timed out")
        time.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL
        stmt = _client().statement_execution.get_statement(stmt.statement_id)

    if stmt.status.state != StatementState.SUCCEEDED:
        err = getattr(stmt.status, "error", None)
        raise RuntimeError(f"Databricks query failed: {err}")

    columns = [c.name for c in stmt.manifest.schema.columns]
    return [dict(zip(columns, row)) for row in (stmt.result.data_array or [])]


def _coerce(v: Any) -> Any:
    """Convert non-JSON-safe types (Decimal, etc.) to native Python."""
    if v is None:
        return None
    try:
        from decimal import Decimal  # noqa: PLC0415
        if isinstance(v, Decimal):
            return float(v)
    except ImportError:
        pass
    return v


def get_patient_hierarchy(patient_id: str, warehouse_id: str) -> dict | None:
    """
    Return a nested hierarchy dict:
      { patient_id, samples: [{ sample_id, cancer_type, …, parts: [{ part_number, …,
        blocks: [{ block_number, …, slides: [{ image_id, stain_name, … }] }] }] }] }
    Returns None if patient is not found in the table.
    """
    sql = _PATIENT_SQL
    params = [_param("patient_id", patient_id)]
    rows = _run_query(sql, warehouse_id, params)
    if not rows:
        return None

    samples: dict[str, dict] = {}

    for r in rows:
        sid   = r.get("SAMPLE_ID_IMPACT") or ""
        pnum  = r.get("PART_NUMBER")
        bnum  = str(r.get("BLOCK_NUMBER") or "")
        pkey  = str(pnum) if pnum is not None else "?"
        slide_url = r.get("slide_path") or ""
        can_serve = slide_url.startswith("s3://")

        if sid not in samples:
            samples[sid] = {
                "sample_id":               sid,
                "cancer_type":             r.get("CANCER_TYPE"),
                "cancer_type_detailed":    r.get("CANCER_TYPE_DETAILED"),
                "oncotree_code":           r.get("ONCOTREE_CODE"),
                "primary_site":            r.get("PRIMARY_SITE"),
                "sample_type":             r.get("SAMPLE_TYPE"),
                "metastatic_site":         r.get("METASTATIC_SITE"),
                "tumor_purity":            _coerce(r.get("TUMOR_PURITY")),
                "oncogenic_mutations":     r.get("ONCOGENIC_MUTATIONS"),
                "num_oncogenic_mutations": _coerce(r.get("NUM_ONCOGENIC_MUTATIONS")),
                "tmb_score":               _coerce(r.get("CVR_TMB_SCORE")),
                "msi_type":                r.get("MSI_TYPE"),
                "parts": {},
            }

        s = samples[sid]

        if pkey not in s["parts"]:
            s["parts"][pkey] = {
                "part_number":      pnum,
                "part_designator":  r.get("part_designator"),
                "part_type":        r.get("part_type"),
                "part_description": r.get("part_description"),
                "subspecialty":     r.get("subspecialty"),
                "path_dx_title":    r.get("PATH_DX_SPEC_TITLE"),
                "blocks": {},
            }

        p = s["parts"][pkey]

        if bnum not in p["blocks"]:
            p["blocks"][bnum] = {
                "block_number": bnum,
                "block_label":  r.get("BLOCK_LABEL"),
                "slides": [],
            }

        p["blocks"][bnum]["slides"].append({
            "image_id":        str(r.get("image_id")),
            "stain_name":      r.get("stain_name"),
            "stain_group":     r.get("stain_group"),
            "is_hne":          str(r.get("IS_HNE","0")) == "1",
            "is_ihc":          str(r.get("IS_IHC","0")) == "1",
            "magnification":   r.get("magnification"),
            "file_size_bytes": _coerce(r.get("file_size_bytes")),
            "can_serve_tiles": can_serve,
            "barcode":         r.get("barcode"),
            "block_label":     r.get("BLOCK_LABEL"),
            "block_number":    r.get("BLOCK_NUMBER"),
            # Part-level anatomical context propagated to slide for frontend display
            "part_description": r.get("part_description"),
        })

    # Clinical sort order for stain groups
    _STAIN_ORDER = {
        "h&e (initial)": 0,
        "h&e (other)":   1,
        "ihc":           2,
    }

    def _slide_sort_key(sl: dict) -> tuple:
        group = (sl.get("stain_group") or "").lower()
        order = _STAIN_ORDER.get(group, 3)
        return (order, (sl.get("stain_name") or "").lower())

    result: list[dict] = []
    for s in samples.values():
        parts_list = []
        for p in s["parts"].values():
            blocks_list = []
            for b in p["blocks"].values():
                b["slides"].sort(key=_slide_sort_key)
                blocks_list.append(b)
            # Also sort blocks so real blocks (with label) come after unblocked slides
            blocks_list.sort(key=lambda b: (0 if not (b.get("block_label") or "").strip() else 1))
            p_out = {k: v for k, v in p.items() if k != "blocks"}
            p_out["blocks"] = blocks_list
            parts_list.append(p_out)
        s_out = {k: v for k, v in s.items() if k != "parts"}
        s_out["parts"] = parts_list
        result.append(s_out)

    return {"patient_id": patient_id, "samples": result}


def get_slide_dbmeta(image_id: str, warehouse_id: str) -> dict | None:
    """Return flat Databricks metadata row for one slide."""
    sql = _SLIDE_SQL
    params = [_param("image_id", str(image_id))]
    rows = _run_query(sql, warehouse_id, params)
    if not rows:
        return None
    return {k: _coerce(v) for k, v in rows[0].items()}


def get_slide_path(image_id: str, warehouse_id: str) -> str | None:
    """Return the S3 URI for a slide given its image_id, or None if not found."""
    rows = _run_query(_SLIDE_PATH_SQL, warehouse_id, [_param("image_id", str(image_id))])
    if not rows:
        return None
    return rows[0].get("path") or None


# ---------------------------------------------------------------------------
# Search / autocomplete
# ---------------------------------------------------------------------------

_SEARCH_PATIENT_SQL = f"""
SELECT DISTINCT PATIENT_ID_IMPACT AS patient_id,
       MAX(CANCER_TYPE) AS cancer_type,
       COUNT(*) AS slide_count
FROM {_TABLE}
WHERE PATIENT_ID_IMPACT LIKE :prefix
GROUP BY patient_id
ORDER BY patient_id
LIMIT 8
"""

_SEARCH_SLIDE_SQL = f"""
SELECT image_id, PATIENT_ID_IMPACT AS patient_id, stain_name
FROM {_TABLE}
WHERE CAST(image_id AS STRING) LIKE :prefix
ORDER BY image_id
LIMIT 8
"""

_SEARCH_SAMPLE_SQL = f"""
SELECT DISTINCT SAMPLE_ID_IMPACT AS sample_id, PATIENT_ID_IMPACT AS patient_id,
       MAX(CANCER_TYPE) AS cancer_type
FROM {_TABLE}
WHERE SAMPLE_ID_IMPACT LIKE :prefix
GROUP BY sample_id, patient_id
ORDER BY sample_id
LIMIT 8
"""


def search_suggestions(query: str, warehouse_id: str) -> list[dict]:
    """
    Return up to 8 autocomplete suggestions for the given query string.

    Detects query type by pattern:
      P-<digits>            → patient suggestions
      P-<digits>-T<digits>  → sample suggestions
      <digits>              → slide image_id suggestions

    Each result has: { type, id, label, sublabel }
    """
    import re  # noqa: PLC0415

    q = query.strip()
    if not q:
        return []

    prefix = q.replace("%", r"\%").replace("_", r"\_") + "%"
    param  = [_param("prefix", prefix)]

    # Sample ID pattern: P-digits-T
    if re.match(r"^P-\d.*-T", q, re.IGNORECASE):
        sql  = _SEARCH_SAMPLE_SQL
        rows = _run_query(sql, warehouse_id, param)
        return [
            {
                "type":     "sample",
                "id":       r["sample_id"],
                "label":    r["sample_id"],
                "sublabel": r.get("cancer_type") or "",
            }
            for r in rows
        ]

    # Patient ID pattern: starts with P-
    if re.match(r"^P-", q, re.IGNORECASE):
        sql  = _SEARCH_PATIENT_SQL
        rows = _run_query(sql, warehouse_id, param)
        return [
            {
                "type":     "patient",
                "id":       r["patient_id"],
                "label":    r["patient_id"],
                "sublabel": f"{r.get('cancer_type') or ''} · {r.get('slide_count', '')} slides".strip(" ·"),
            }
            for r in rows
        ]

    # Numeric → slide image_id
    if re.match(r"^\d", q):
        sql  = _SEARCH_SLIDE_SQL
        rows = _run_query(sql, warehouse_id, param)
        return [
            {
                "type":     "slide",
                "id":       str(r["image_id"]),
                "label":    str(r["image_id"]),
                "sublabel": f"{r.get('patient_id') or ''} · {r.get('stain_name') or ''}".strip(" ·"),
            }
            for r in rows
        ]

    return []


# ---------------------------------------------------------------------------
# Slide summary (Phase 7)
# ---------------------------------------------------------------------------

def get_sample_slide_summary(
    sample_ids: list[str],
    warehouse_id: str,
) -> list[dict]:
    """
    Return pre-computed slide availability stats for the given sample IDs.

    Reads from the ``sample_wsi_summary`` Delta table, which is populated
    nightly by the Databricks Asset Bundle job (``wsi-summary-pipeline``).

    Each result dict has:
      sample_id, patient_id, servable_slide_count, has_hne, has_ihc, stain_types

    Samples not present in the summary table are silently omitted — the caller
    (generate_wsi_clinical_attrs.py) fills in zero-count rows for them.
    """
    if not sample_ids:
        return []

    placeholders = ", ".join(f"'{sid.replace(chr(39), '')}'" for sid in sample_ids)
    sql = f"""
SELECT
    sample_id,
    patient_id,
    servable_slide_count,
    has_hne,
    has_ihc,
    stain_types
FROM {_SUMMARY}
WHERE sample_id IN ({placeholders})
ORDER BY sample_id
"""
    rows = _run_query(sql, warehouse_id)
    return [
        {
            "sample_id":            r.get("sample_id"),
            "patient_id":           r.get("patient_id"),
            "servable_slide_count": int(r.get("servable_slide_count") or 0),
            "has_hne":              int(r.get("has_hne") or 0),
            "has_ihc":              int(r.get("has_ihc") or 0),
            "stain_types":          r.get("stain_types") or "",
        }
        for r in rows
    ]
