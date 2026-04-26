"""Render the Kanagawa dashboard HTML from data fetched by fetch_estat.py.

Reads ``data/_summary.json`` plus ``data/<id>.json`` for successful indicators,
and writes ``docs/index.html`` using the Jinja2 template at
``templates/index.html.j2``.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "stats.yaml"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
TEMPLATE_DIR = ROOT / "templates"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("build_site")

JST = timezone(timedelta(hours=9))


def fmt_value(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:,.1f}"
    return f"{v:.2f}"


def main() -> int:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    summary = json.loads((DATA_DIR / "_summary.json").read_text(encoding="utf-8"))

    indicators_view: list[dict] = []
    for s in summary:
        view = {
            "id": s["id"],
            "name": s["name"],
            "category": s["category"],
            "success": s["success"],
            "error": s.get("error"),
        }
        if s["success"]:
            payload_path = DATA_DIR / f"{s['id']}.json"
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            view["payload"] = payload
            view["latest_value"] = fmt_value(payload["values"][-1] if payload["values"] else None)
            view["latest_label"] = payload["labels"][-1] if payload["labels"] else ""
        indicators_view.append(view)

    indicators_by_category: "OrderedDict[str, list[dict]]" = OrderedDict()
    for ind in indicators_view:
        if ind["success"]:
            indicators_by_category.setdefault(ind["category"], []).append(ind)

    chart_data = {
        ind["id"]: {
            "labels": ind["payload"]["labels"],
            "values": ind["payload"]["values"],
            "name": ind["payload"]["name"],
            "chart": ind["payload"]["chart"],
            "unit": ind["payload"]["unit"],
        }
        for ind in indicators_view
        if ind["success"]
    }

    failed = [
        {"name": ind["name"], "category": ind["category"], "error": ind.get("error", "")}
        for ind in indicators_view
        if not ind["success"]
    ]

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("index.html.j2")
    html = template.render(
        generated_at=datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        indicators=indicators_view,
        indicators_by_category=indicators_by_category,
        chart_data_json=json.dumps(chart_data, ensure_ascii=False),
        failed=failed,
        external_links=config.get("external_links", []),
        japan_dashboard=config.get("japan_dashboard"),
    )

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    out = DOCS_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    log.info("Wrote %s (%d indicators, %d failed)", out, len(indicators_by_category and chart_data), len(failed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
