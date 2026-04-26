"""Fetch Kanagawa-related statistics from the e-Stat API.

For each indicator declared in ``config/stats.yaml``:
1. Resolve a concrete ``statsDataId`` by listing tables under ``statsCode`` and
   matching the configured ``titleMatch`` regex (most recent update wins).
2. Pull data with the configured filters (``cdArea``, ``cdCat01`` etc).
3. Normalize to ``{labels: [...], values: [...], meta: {...}}`` and write to
   ``data/<indicator_id>.json``.

Failures for a single indicator are logged but do not abort the run, so the
dashboard always builds with whatever succeeded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "stats.yaml"
DATA_DIR = ROOT / "data"
API_BASE = "https://api.e-stat.go.jp/rest/3.0/app/json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fetch_estat")


@dataclass
class FetchResult:
    indicator_id: str
    name: str
    category: str
    success: bool
    error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


def get_app_id() -> str:
    app_id = os.environ.get("ESTAT_APP_ID")
    if not app_id:
        raise SystemExit("ESTAT_APP_ID is not set. Put it in .env or export it.")
    return app_id


def http_get(url: str, params: dict[str, Any], retries: int = 3) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            wait = 2 ** attempt
            log.warning("HTTP error (%s); retrying in %ss", e, wait)
            time.sleep(wait)
    raise RuntimeError(f"Giving up after {retries} attempts: {last_exc}")


def find_stats_data_id(app_id: str, stats_code: str, title_match: str | None) -> tuple[str, dict[str, Any]]:
    """Return (statsDataId, table_info) for the most recently updated table
    matching ``title_match`` under ``stats_code``."""
    params = {
        "appId": app_id,
        "statsCode": stats_code,
        "searchKind": 1,
        "limit": 100,
    }
    res = http_get(f"{API_BASE}/getStatsList", params)
    status = res.get("GET_STATS_LIST", {}).get("RESULT", {}).get("STATUS")
    if status != 0:
        raise RuntimeError(f"getStatsList failed: {res.get('GET_STATS_LIST', {}).get('RESULT')}")

    tables = res.get("GET_STATS_LIST", {}).get("DATALIST_INF", {}).get("TABLE_INF", [])
    if isinstance(tables, dict):
        tables = [tables]
    if not tables:
        raise RuntimeError(f"No tables found for statsCode={stats_code}")

    if title_match:
        regex = re.compile(title_match)

        def _title(t: dict[str, Any]) -> str:
            title = t.get("TITLE", "")
            if isinstance(title, dict):
                title = title.get("$", "")
            stat_name = t.get("STAT_NAME", {})
            if isinstance(stat_name, dict):
                stat_name = stat_name.get("$", "")
            return f"{stat_name} {title}"

        candidates = [t for t in tables if regex.search(_title(t))]
        if not candidates:
            log.warning(
                "No tables matched title regex %r for statsCode=%s; falling back to most-recent table",
                title_match,
                stats_code,
            )
            candidates = tables
    else:
        candidates = tables

    candidates.sort(key=lambda t: t.get("UPDATED_DATE", ""), reverse=True)
    chosen = candidates[0]
    return str(chosen["@id"]), chosen


def fetch_stats_data(app_id: str, stats_data_id: str, filters: dict[str, Any]) -> dict[str, Any]:
    params = {
        "appId": app_id,
        "statsDataId": stats_data_id,
        "metaGetFlg": "Y",
        "cntGetFlg": "N",
        "explanationGetFlg": "N",
        "annotationGetFlg": "N",
        "lang": "J",
    }
    params.update(filters or {})
    res = http_get(f"{API_BASE}/getStatsData", params)
    status = res.get("GET_STATS_DATA", {}).get("RESULT", {}).get("STATUS")
    if status != 0:
        raise RuntimeError(f"getStatsData failed: {res.get('GET_STATS_DATA', {}).get('RESULT')}")
    return res["GET_STATS_DATA"]


def _index_class_obj(class_obj: list[dict[str, Any]] | dict[str, Any]) -> dict[str, dict[str, str]]:
    """Build {classId -> {code -> label}} from CLASS_INF.CLASS_OBJ."""
    if isinstance(class_obj, dict):
        class_obj = [class_obj]
    out: dict[str, dict[str, str]] = {}
    for c in class_obj:
        cid = c.get("@id")
        cls = c.get("CLASS", [])
        if isinstance(cls, dict):
            cls = [cls]
        out[cid] = {x.get("@code"): x.get("@name", "") for x in cls}
    return out


_TOTAL_KEYWORDS = ("総数", "総計", "総人口", "総合計", "合計", "全体", "全国")


def _full_class_obj(class_inf: dict[str, Any]) -> list[dict[str, Any]]:
    cls_obj = class_inf.get("CLASS_OBJ", [])
    if isinstance(cls_obj, dict):
        cls_obj = [cls_obj]
    return cls_obj


def _pick_total_code(class_obj: dict[str, Any]) -> str | None:
    """Pick the 'total' code for a non-time dimension.

    Heuristic: prefer a class entry whose @name contains 総数/計 etc.,
    otherwise return the first code.
    """
    classes = class_obj.get("CLASS", [])
    if isinstance(classes, dict):
        classes = [classes]
    if not classes:
        return None
    for c in classes:
        name = c.get("@name", "")
        if any(k in name for k in _TOTAL_KEYWORDS):
            return c.get("@code")
    return classes[0].get("@code")


def normalize(stats_data: dict[str, Any], indicator: dict[str, Any]) -> dict[str, Any]:
    """Pull (time, value) pairs from a getStatsData response.

    Auto-selects a "total" code for each non-time non-area dimension so that
    we get a single value per time point. Series sorted ascending by time code.
    """
    statistical_data = stats_data["STATISTICAL_DATA"]
    table_inf = statistical_data.get("TABLE_INF", {})
    class_inf = statistical_data.get("CLASS_INF", {})
    data_inf = statistical_data.get("DATA_INF", {})

    full_class_obj = _full_class_obj(class_inf)
    classes = _index_class_obj(class_inf.get("CLASS_OBJ", []))

    time_class_id = None
    for cid in classes:
        if "time" in cid.lower() or "時間" in cid:
            time_class_id = cid
            break
    if time_class_id is None and classes:
        time_class_id = next(iter(classes))

    # For every non-time, non-area dimension, decide which code = "total"
    fixed_codes: dict[str, str] = {}
    for c in full_class_obj:
        cid = c.get("@id")
        if cid in (time_class_id, "area"):
            continue
        # already fixed via filter? then no need to pick
        if f"cd{cid.capitalize()}" in (indicator.get("filters") or {}):
            continue
        chosen = _pick_total_code(c)
        if chosen:
            fixed_codes[cid] = chosen

    values = data_inf.get("VALUE", [])
    if isinstance(values, dict):
        values = [values]

    scale = float(indicator.get("scale", 1) or 1)

    pairs: list[tuple[str, float, str]] = []
    for v in values:
        # Skip rows that don't match the chosen total combo
        skip = False
        for cid, code in fixed_codes.items():
            if v.get(f"@{cid}") != code:
                skip = True
                break
        if skip:
            continue
        time_code = v.get(f"@{time_class_id}") if time_class_id else None
        time_label = (
            classes.get(time_class_id, {}).get(time_code, time_code) if time_class_id else ""
        )
        raw = v.get("$")
        if raw in (None, "", "-", "...", "*"):
            continue
        try:
            num = float(raw) * scale
        except ValueError:
            continue
        pairs.append((time_code or "", num, time_label or ""))

    pairs.sort(key=lambda p: p[0])

    # Final dedupe by time (keep last) — defensive
    by_time: "dict[str, tuple[float, str]]" = {}
    for tc, num, lbl in pairs:
        by_time[tc] = (num, lbl)
    pairs = [(tc, *by_time[tc]) for tc in by_time]
    pairs.sort(key=lambda p: p[0])

    labels = [p[2] or p[0] for p in pairs]
    series = [p[1] for p in pairs]

    table_title = table_inf.get("TITLE", "")
    if isinstance(table_title, dict):
        table_title = table_title.get("$", "")

    return {
        "labels": labels,
        "values": series,
        "table_title": str(table_title),
        "stats_data_id": str(table_inf.get("@id", "")),
        "updated": table_inf.get("UPDATED_DATE", ""),
        "source_url": f"https://www.e-stat.go.jp/dbview?sid={table_inf.get('@id', '')}",
        "raw_count": len(values),
    }


def process_indicator(app_id: str, indicator: dict[str, Any]) -> FetchResult:
    iid = indicator["id"]
    log.info("Fetching %s (%s)", iid, indicator["name"])
    try:
        if indicator.get("statsDataId"):
            stats_data_id = str(indicator["statsDataId"])
            log.info("  -> using explicit statsDataId=%s", stats_data_id)
        else:
            stats_data_id, _table = find_stats_data_id(
                app_id,
                indicator["statsCode"],
                indicator.get("titleMatch"),
            )
            log.info("  -> resolved statsDataId=%s", stats_data_id)
        raw = fetch_stats_data(app_id, stats_data_id, indicator.get("filters", {}))
        norm = normalize(raw, indicator)
        if not norm["values"]:
            raise RuntimeError("No values in response after filtering")
        payload = {
            "id": iid,
            "name": indicator["name"],
            "category": indicator["category"],
            "description": indicator.get("description", ""),
            "source": indicator.get("source", ""),
            "chart": indicator.get("chart", "line"),
            "unit": indicator.get("unit", ""),
            **norm,
        }
        return FetchResult(iid, indicator["name"], indicator["category"], True, payload=payload)
    except Exception as e:  # noqa: BLE001
        log.error("  FAILED for %s: %s", iid, e)
        return FetchResult(iid, indicator["name"], indicator["category"], False, error=str(e))


def main() -> int:
    app_id = get_app_id()
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    summary: list[dict[str, Any]] = []
    for indicator in config.get("indicators", []):
        result = process_indicator(app_id, indicator)
        if result.success:
            (DATA_DIR / f"{result.indicator_id}.json").write_text(
                json.dumps(result.payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        summary.append({
            "id": result.indicator_id,
            "name": result.name,
            "category": result.category,
            "success": result.success,
            "error": result.error,
        })

    (DATA_DIR / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    success_count = sum(1 for s in summary if s["success"])
    log.info("Done: %d/%d indicators succeeded", success_count, len(summary))
    return 0 if success_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
