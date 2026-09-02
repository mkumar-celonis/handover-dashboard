from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from scanner.env import load_dotenv_files
from scanner.score import rag_from_score

SITE_DEFAULT = "https://celonis.atlassian.net"
PROJECT_KEY = "TMT"

# Jira component / CBE Service-internal values for Task Mining services.
SERVICE_MAP: dict[str, dict[str, Any]] = {
    "task-mining": {
        "component": "TM Cloud Service",
        "cbe": ["tm-cloud-service", "cloud-task-mining", "task-mining"],
    },
    "task-mining-ai": {
        "component": "TM AI Service",
        "cbe": ["tm-ai-service", "task-mining-ai"],
    },
    "task-mining-uploader": {
        "component": "TM Uploader",
        "cbe": ["tm-uploader", "task-mining-uploader"],
    },
    "tm-image-collector": {
        "component": "TM Image Collector",
        "cbe": ["tm-image-collector"],
    },
    "task-mining-gateway": {
        "component": "TM Gateway",
        "cbe": ["tm-gateway", "task-mining-gateway"],
    },
}


def credentials() -> dict[str, str] | None:
    load_dotenv_files()
    email = (os.environ.get("JIRA_EMAIL") or os.environ.get("JIRA_USER") or "").strip()
    token = (os.environ.get("JIRA_TOKEN") or os.environ.get("JIRA_API_TOKEN") or "").strip()
    if not email or not token:
        return None
    site = (os.environ.get("JIRA_SITE") or SITE_DEFAULT).rstrip("/")
    timeout = str(float(os.environ.get("HTTP_TIMEOUT", "45")))
    return {"email": email, "token": token, "site": site, "timeout": timeout}


def _service_key(catalog: dict[str, Any] | None, service_id: str | None) -> str:
    name = str((catalog or {}).get("name") or service_id or "").strip().lower()
    if name in SERVICE_MAP:
        return name
    for key in SERVICE_MAP:
        if key in name or name in key:
            return key
    return name


def _post(creds: dict[str, str], path: str, body: dict[str, Any]) -> tuple[int, Any]:
    url = f"{creds['site']}{path}"
    token = f"{creds['email']}:{creds['token']}"
    import base64

    auth = base64.b64encode(token.encode()).decode()
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "handover-dashboard",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(creds.get("timeout") or 45)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:200]
        try:
            parsed = json.loads(err) if err else {}
        except json.JSONDecodeError:
            parsed = {"message": err}
        return exc.code, parsed


def _count(creds: dict[str, str], jql: str) -> int | None:
    code, data = _post(creds, "/rest/api/3/search/approximate-count", {"jql": jql})
    if code == 200 and isinstance(data, dict) and data.get("count") is not None:
        try:
            return int(data["count"])
        except (TypeError, ValueError):
            return None
    code, data = _post(
        creds,
        "/rest/api/3/search/jql",
        {"jql": jql, "maxResults": 1, "fields": ["id"]},
    )
    if code == 200 and isinstance(data, dict):
        issues = data.get("issues") or []
        if data.get("isLast") and isinstance(issues, list):
            return len(issues)
        total = data.get("total")
        if total is not None:
            try:
                return int(total)
            except (TypeError, ValueError):
                return None
    return None


def _in_quotes(values: list[str]) -> str:
    return ", ".join(f'"{v}"' for v in values if v)


def assess_jira(
    catalog: dict[str, Any] | None = None,
    service_id: str | None = None,
    window_days: int = 90,
) -> dict[str, Any]:
    days = min(max(int(window_days or 90), 1), 400)
    key = _service_key(catalog, service_id)
    mapping = SERVICE_MAP.get(key) or {"component": (catalog or {}).get("title") or key, "cbe": [key]}
    component = mapping["component"]
    cbe_vals = mapping.get("cbe") or [key]
    site = SITE_DEFAULT
    creds = credentials()
    browse = f"{site}/issues/?jql="
    if not creds:
        return {
            "status": "disconnected",
            "rag": "amber",
            "title": "Not connected",
            "score": None,
            "summary": "Set JIRA_EMAIL and JIRA_TOKEN to count CBE / security / feature / bug tickets.",
            "url": site,
            "window_days": days,
            "signals": {},
            "why": ["Jira is not connected. Put JIRA_EMAIL and JIRA_TOKEN in .env (or the sibling onboarding-readiness .env)."],
        }

    site = creds["site"]
    created = f"created >= -{days}d"
    cbe_scope = f'"Service internal" in ({_in_quotes(cbe_vals)})'
    queries = {
        "cbe": f'project = CBE AND {cbe_scope} AND issuetype != Vulnerability AND {created}',
        "security": f'project = CBE AND {cbe_scope} AND issuetype = Vulnerability AND {created}',
        "features": f'project = {PROJECT_KEY} AND component = "{component}" AND issuetype in (Story, Epic) AND {created}',
        "bugs": f'project = {PROJECT_KEY} AND component = "{component}" AND issuetype = Bug AND {created}',
    }
    counts: dict[str, int | None] = {}
    errors: list[str] = []
    for name, jql in queries.items():
        try:
            counts[name] = _count(creds, jql)
        except Exception as exc:
            counts[name] = None
            errors.append(f"{name}: {exc}"[:160])

    if all(v is None for v in counts.values()):
        return {
            "status": "error",
            "rag": "red",
            "title": "Jira call failed",
            "score": None,
            "summary": (errors[0] if errors else "Could not count Jira issues."),
            "url": site,
            "window_days": days,
            "signals": {},
            "why": errors or ["Check JIRA_EMAIL / JIRA_TOKEN and that the token can read TMT and CBE."],
        }

    cbe_n = counts.get("cbe") or 0
    sec_n = counts.get("security") or 0
    feat_n = counts.get("features") or 0
    bug_n = counts.get("bugs") or 0
    score = 4.0
    if sec_n >= 5 or cbe_n >= 10:
        score -= 1.2
    elif sec_n or cbe_n >= 3:
        score -= 0.6
    if bug_n >= 20:
        score -= 0.5
    score = max(0.0, min(5.0, round(score, 1)))
    rag = rag_from_score(score)
    why = [
        f"Last {days}d for {component}: {bug_n} bugs, {feat_n} features, {cbe_n} CBE, {sec_n} security vulns.",
        f"TMT component `{component}`. CBE Service internal in {', '.join(cbe_vals)}.",
    ]
    if errors:
        why.append(errors[0])
    ui = (
        f"{site}/issues/?jql="
        + urllib.parse.quote(f'project = {PROJECT_KEY} AND component = "{component}" AND {created}')
    )
    return {
        "status": "connected",
        "rag": rag,
        "title": "Connected",
        "score": score,
        "summary": f"{bug_n} bugs · {feat_n} features · {cbe_n} CBE · {sec_n} security in {days}d.",
        "url": ui,
        "window_days": days,
        "signals": {
            "cbe": cbe_n,
            "security": sec_n,
            "features": feat_n,
            "bugs": bug_n,
            "component": component,
        },
        "links": {name: f"{site}/issues/?jql={urllib.parse.quote(jql)}" for name, jql in queries.items()},
        "why": why,
    }
