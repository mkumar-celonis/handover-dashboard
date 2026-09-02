from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from scanner.env import load_dotenv_files
from scanner.score import rag_from_score

TEAM = "https://celocore.us-2.celonis.cloud"
SPACE_ID = "f88d799c-2272-4a3c-8335-29c05d37acdf"
PACKAGE_ID = "cd114198-b0ef-431b-ad0b-b19455502e5f"
VIEW_NODE_ID = "ad1b7411-7f8a-47c8-980e-2dae22373407"
METRICS_ASSET_ID = "5655bef6-d3fe-4e48-85bb-cdddaffa09ce"
VIEW_URL = (
    f"{TEAM}/package-manager/ui/views/ui/spaces/{SPACE_ID}/packages/{PACKAGE_ID}"
    f"/nodes/{VIEW_NODE_ID}?activeTabs=code-purple-metrics:{METRICS_ASSET_ID}"
)
PROFILE_KEYS = f"{TEAM}/ui/team/settings/applications"
USER_KEYS = f"{TEAM}/ui/team#/profile"

GIT_SCORE = {"adopted": 4.5, "partial": 3.0, "missing": 1.0, "na": None}


def credentials() -> dict[str, str] | None:
    load_dotenv_files()
    token = (
        os.environ.get("CELONIS_API_TOKEN")
        or os.environ.get("CELONIS_USER_API_KEY")
        or os.environ.get("CELOCORE_API_TOKEN")
        or ""
    ).strip()
    app_key = (os.environ.get("CELONIS_APP_KEY") or os.environ.get("CELONIS_APPLICATION_KEY") or "").strip()
    team = (os.environ.get("CELONIS_TEAM") or TEAM).strip().rstrip("/")
    timeout = str(float(os.environ.get("HTTP_TIMEOUT", "45")))
    if token:
        return {"auth": "bearer", "token": token, "team": team, "timeout": timeout}
    if app_key:
        return {"auth": "appkey", "token": app_key, "team": team, "timeout": timeout}
    return None


def _headers(creds: dict[str, str]) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if creds.get("auth") == "appkey":
        headers["Authorization"] = f"AppKey {creds['token']}"
    else:
        headers["Authorization"] = f"Bearer {creds['token']}"
    return headers


def _get(creds: dict[str, str], path: str, params: dict[str, Any] | None = None) -> Any:
    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"{creds['team']}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers=_headers(creds), method="GET")
    timeout = float(creds.get("timeout") or 45)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"Celonis HTTP {exc.code} on {path}: {err_body}") from exc


def _needles(catalog: dict[str, Any]) -> list[str]:
    names = [
        str(catalog.get("name") or ""),
        str(catalog.get("title") or ""),
        "task-mining",
        "task mining",
        "cloud-task-mining",
        "taskmining",
    ]
    out: list[str] = []
    for name in names:
        text = name.strip().lower().replace("_", "-")
        if text and text not in out:
            out.append(text)
    return out


def _hay(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value).lower()
    return str(value).lower()


def _matches_service(row: Any, needles: list[str]) -> bool:
    blob = _hay(row)
    return any(n in blob for n in needles)


def _as_list(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("content", "data", "items", "nodes", "kpis", "records", "results"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    return []


def _node_content(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    for key in ("serializedContent", "serialized_content", "content", "yaml"):
        val = node.get(key)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, dict):
            return json.dumps(val)
    return json.dumps(node)


def _extract_km_keys(blob: str) -> list[str]:
    keys: list[str] = []
    for pattern in (
        r"knowledgeModelKey['\":\s]+([A-Za-z0-9_.\-]+)",
        r"knowledgeModelId['\":\s]+([A-Za-z0-9_.\-]+)",
        r"knowledge-models/([A-Za-z0-9_.\-]+)",
        r'"id"\s*:\s*"([A-Za-z0-9_.\-]*purple[A-Za-z0-9_.\-]*)"',
    ):
        for match in re.finditer(pattern, blob, re.I):
            key = match.group(1).strip().strip("'\"")
            if key and key not in keys:
                keys.append(key)
    return keys[:8]


def _to_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _kpi_score(values: list[float]) -> float | None:
    if not values:
        return None
    normalized: list[float] = []
    for raw in values:
        if 0 <= raw <= 1:
            normalized.append(raw * 5)
        elif raw > 5:
            normalized.append(min(raw, 100) / 20)
        else:
            normalized.append(raw)
    if not normalized:
        return None
    return round(max(0.0, min(5.0, sum(normalized) / len(normalized))), 1)


def _flatten_kpis(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), list) else None
        if data is None:
            for item in _as_list(payload):
                if isinstance(item, dict):
                    rows.append(item)
        else:
            headers = [str(h.get("id") or h.get("name") or "") for h in (payload.get("headers") or [])]
            for item in data:
                if isinstance(item, dict):
                    rows.append(item)
                elif isinstance(item, list) and headers:
                    rows.append({headers[i]: item[i] for i in range(min(len(headers), len(item)))})
    elif isinstance(payload, list):
        rows = [x for x in payload if isinstance(x, dict)]
    return rows


def _row_metrics(row: dict[str, Any]) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for key, val in row.items():
        num = _to_number(val)
        if num is None:
            continue
        name = str(key)
        low = name.lower()
        if any(tok in low for tok in ("id", "year", "week", "count", "timestamp")) and "rate" not in low and "pct" not in low:
            if "cover" not in low and "score" not in low and "purple" not in low:
                continue
        out.append((name, num))
    return out


def assess_code_purple_metrics(catalog: dict[str, Any]) -> dict[str, Any]:
    creds = credentials()
    base = {
        "id": "code_purple_live",
        "label": "Code Purple metrics",
        "url": VIEW_URL,
        "service": str(catalog.get("name") or catalog.get("title") or ""),
    }
    if not creds:
        return {
            **base,
            "status": "disconnected",
            "rag": "amber",
            "title": "Not connected",
            "score": None,
            "summary": "Set CELONIS_API_TOKEN to pull live Code Purple metrics from Celocore.",
            "why": [
                "This is a logged-in Studio view, not a public dashboard.",
                "Create a User API key (Edit Profile) or Application key on celocore.us-2 and grant USE PACKAGE on this Studio package.",
            ],
            "setup": [
                f"Open {VIEW_URL}",
                f"Create a User API key at {USER_KEYS} (or an Application key at {PROFILE_KEYS}).",
                "Put CELONIS_API_TOKEN=... in handover-dashboard/.env (Bearer). Application keys use CELONIS_APP_KEY.",
            ],
            "metrics": [],
            "gap": {
                "id": "platform.code_purple.live",
                "area": "release",
                "title": "Code Purple metrics not connected",
                "ask": "Connect Celocore so handover scoring includes live Code Purple coverage, not just git workflow files.",
                "p0": False,
            },
        }

    errors: list[str] = []
    node: Any = {}
    asset: Any = {}
    try:
        node = _get(creds, f"/package-manager/api/nodes/{VIEW_NODE_ID}")
    except Exception as exc:
        errors.append(str(exc))
    try:
        asset = _get(creds, f"/package-manager/api/nodes/{METRICS_ASSET_ID}")
    except Exception as exc:
        errors.append(str(exc))

    blob = _node_content(node) + "\n" + _node_content(asset)
    km_keys = _extract_km_keys(blob)
    if not km_keys:
        try:
            models = _as_list(_get(creds, "/intelligence/api/knowledge-models", {"pageSize": 100}))
            for model in models:
                name = _hay(model.get("name") or "") + " " + _hay(model.get("id") or "")
                if "purple" in name or "code quality" in name or "coverage" in name:
                    mid = str(model.get("id") or "")
                    if mid:
                        km_keys.append(mid)
        except Exception as exc:
            errors.append(str(exc))

    rows: list[dict[str, Any]] = []
    used_km = ""
    needles = _needles(catalog)
    for km in km_keys:
        try:
            kpis_meta = _as_list(_get(creds, f"/intelligence/api/knowledge-models/{urllib.parse.quote(km, safe='._-')}/kpis"))
        except Exception as exc:
            errors.append(str(exc))
            kpis_meta = []
        kpi_ids = [str(k.get("id") or k.get("name") or "") for k in kpis_meta if isinstance(k, dict)]
        kpi_ids = [k for k in kpi_ids if k][:20]
        params: dict[str, Any] = {"pageSize": 100}
        if kpi_ids:
            params["kpis"] = ",".join(kpi_ids)
        try:
            payload = _get(creds, f"/intelligence/api/knowledge-models/{urllib.parse.quote(km, safe='._-')}/data", params)
        except Exception as exc:
            errors.append(str(exc))
            continue
        found = _flatten_kpis(payload)
        matched = [r for r in found if _matches_service(r, needles)]
        pick = matched or found
        if pick:
            rows = pick
            used_km = km
            if matched:
                break

    if errors and not rows and not node and not asset:
        return {
            **base,
            "status": "error",
            "rag": "red",
            "title": "Celonis call failed",
            "score": None,
            "summary": errors[0][:300],
            "why": ["Could not read the Code Purple Studio view. Check CELONIS_API_TOKEN and USE PACKAGE on the Celocore package."],
            "setup": [f"Open {VIEW_URL}", f"Keys: {USER_KEYS}"],
            "errors": errors[:4],
            "metrics": [],
            "gap": {
                "id": "platform.code_purple.live",
                "area": "release",
                "title": "Code Purple API failed",
                "ask": "Fix Celocore auth (CELONIS_API_TOKEN Bearer or CELONIS_APP_KEY) and grant USE PACKAGE.",
                "p0": False,
            },
        }

    metrics: list[dict[str, Any]] = []
    values: list[float] = []
    for row in rows[:12]:
        for name, num in _row_metrics(row)[:6]:
            metrics.append({"name": name, "value": num, "row": {k: row[k] for k in list(row)[:8]}})
            values.append(num)
    score = _kpi_score(values)
    rag = rag_from_score(score) if score is not None else "amber"
    matched_n = sum(1 for r in rows if _matches_service(r, needles))
    why: list[str] = []
    if score is None:
        why.append("Connected to Celocore, but no numeric Code Purple KPIs matched this service yet.")
        if km_keys:
            why.append(f"Knowledge models seen: {', '.join(km_keys[:3])}.")
        if errors:
            why.append(errors[0][:180])
        status = "partial"
        title = "Connected — no service row"
        summary = "Live view reachable; no scored KPI row for this catalog name."
    else:
        status = "connected"
        title = "Connected"
        scope = "this service" if matched_n else "package-level (no exact service row)"
        summary = f"Live Code Purple {score}/5 from Celocore ({scope}). {len(metrics)} KPI value(s)."
        if rag == "red":
            why.append(f"Red because live Code Purple is {score}/5 (below 3.0). Green needs ≥4.0 (~80% coverage / quality).")
        elif rag == "amber":
            why.append(f"Amber because live Code Purple is {score}/5. Green needs ≥4.0.")
        else:
            why.append(f"Green: live Code Purple is {score}/5.")
        if not matched_n:
            why.append("No row matched task-mining by name — showing package-level metrics.")
        for item in metrics[:4]:
            why.append(f"{item['name']}: {item['value']}")

    return {
        **base,
        "status": status,
        "rag": rag,
        "title": title,
        "score": score,
        "summary": summary,
        "why": why[:6],
        "metrics": metrics[:12],
        "knowledge_model": used_km,
        "errors": errors[:4],
        "signals": {"rows": len(rows), "matched": matched_n, "kpis": len(metrics)},
        "gap": None
        if score is not None and score >= 3.0
        else {
            "id": "platform.code_purple.live",
            "area": "release",
            "title": "Live Code Purple below handover bar",
            "ask": "Raise Code Purple coverage/quality on the Celocore dashboard before treating the purple lane as inherited.",
            "p0": score is not None and score < 2.0,
        },
    }


def apply_code_purple_live(git_item: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """Overlay Celocore metrics onto the git Code Purple row."""
    merged = dict(git_item or {})
    merged["url"] = live.get("url") or VIEW_URL
    merged["live"] = {k: live.get(k) for k in ("status", "score", "title", "summary", "why", "metrics", "errors")}
    git_status = str(merged.get("status") or "")
    git_pts = GIT_SCORE.get(git_status)
    live_score = live.get("score")
    if live.get("status") == "connected" and live_score is not None and git_pts is not None:
        mixed = round(git_pts * 0.55 + float(live_score) * 0.45, 1)
        merged["score"] = mixed
        merged["git_score"] = git_pts
        merged["live_score"] = live_score
        merged["rag"] = rag_from_score(mixed)
        merged["title"] = f"{merged.get('title') or git_status} · live {live_score}/5"
        merged["summary"] = f"{merged.get('summary') or ''} Live Celocore {live_score}/5 blended with git {git_pts}/5.".strip()
        merged["why"] = [
            f"Git Code Purple is {git_status} ({git_pts}/5). Live Celocore metrics are {live_score}/5. Blended {mixed}/5 (55% git / 45% live).",
            *(live.get("why") or [])[:4],
        ]
    else:
        merged["score"] = git_pts
        live_status = str(live.get("status") or "")
        git_title = merged.get("title") or git_status
        if live_status == "disconnected":
            merged["title"] = f"{git_title} · metrics not connected"
            merged["why"] = [
                f"Git Code Purple is {git_status}. Live Celocore metrics need CELONIS_API_TOKEN.",
                *(live.get("why") or [])[:3],
            ]
        elif live_status in {"error", "partial"}:
            merged["title"] = f"{git_title} · {live.get('title')}"
            merged["rag"] = live.get("rag") or merged.get("rag")
            merged["why"] = live.get("why") or []
            extra = live.get("summary") or ""
            if extra and extra not in (merged.get("summary") or ""):
                merged["summary"] = f"{merged.get('summary') or ''} {extra}".strip()
        if live.get("setup"):
            merged["setup"] = live.get("setup")
    if live.get("gap") and live.get("status") in {"error", "connected"} and live.get("rag") == "red":
        merged["gap"] = live.get("gap") or merged.get("gap")
    return merged
