"""Celonis Confluence (celospace) — live documentation / runbook / SOP quality."""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any

from scanner.env import load_dotenv_files
from scanner.score import rag_from_score, recompute_overall

SITE_DEFAULT = "https://celonis-confluence.atlassian.net"
SPACE_DEFAULT = "DKB"
# Engineering Platform → Task Mining hub (children: Knowledge Base, Way of Working,
# Squad Test Plan, Handover documents, Metrics and SLOs).
HUB_ID_DEFAULT = "17674883"
HUB_PATH = "/wiki/spaces/DKB/pages/17674883/Task+Mining"

STUB_TITLE = re.compile(
    r"\[(?:wip|temp|outdated|archive)\]|\b(?:wip|temp|todo|tbd|outdated|placeholder)\b",
    re.I,
)
RUNBOOK_RE = re.compile(
    r"runbook|incident|on-call|oncall|sev-?1|postmortem|pager|slo|alert|metrics and slos",
    re.I,
)
ARCH_RE = re.compile(r"architecture|adr|design doc|data flow|threat model", re.I)
SOP_RE = re.compile(
    r"sop|onboarding|handover|how-to|how to|checklist|way of working|knowledge base|"
    r"test plan|release process|release strategy|studio package",
    re.I,
)
HTML_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def credentials() -> dict[str, str] | None:
    load_dotenv_files()
    email = (
        os.environ.get("CONFLUENCE_EMAIL")
        or os.environ.get("JIRA_EMAIL")
        or os.environ.get("JIRA_USER")
        or ""
    ).strip()
    token = (
        os.environ.get("CONFLUENCE_TOKEN")
        or os.environ.get("JIRA_TOKEN")
        or os.environ.get("JIRA_API_TOKEN")
        or ""
    ).strip()
    if not email or not token:
        return None
    site = (os.environ.get("CONFLUENCE_SITE") or SITE_DEFAULT).rstrip("/")
    space = (os.environ.get("CONFLUENCE_SPACE") or SPACE_DEFAULT).strip() or SPACE_DEFAULT
    ancestor = (os.environ.get("CONFLUENCE_ANCESTOR") or HUB_ID_DEFAULT).strip() or HUB_ID_DEFAULT
    timeout = str(float(os.environ.get("HTTP_TIMEOUT", "45")))
    return {
        "email": email,
        "token": token,
        "site": site,
        "space": space,
        "ancestor": ancestor,
        "timeout": timeout,
    }


def _headers(creds: dict[str, str]) -> dict[str, str]:
    raw = base64.b64encode(f"{creds['email']}:{creds['token']}".encode()).decode()
    return {"Authorization": f"Basic {raw}", "Accept": "application/json"}


def _get(creds: dict[str, str], path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{creds['site']}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers=_headers(creds), method="GET")
    timeout = float(creds.get("timeout") or 45)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:240]
        raise RuntimeError(f"Confluence HTTP {exc.code} on {path}: {err}") from exc


def _plain(html: str) -> str:
    text = HTML_RE.sub(" ", html or "")
    return WS_RE.sub(" ", text).strip()


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _page_url(site: str, page: dict[str, Any]) -> str:
    webui = ((page.get("_links") or {}).get("webui") or "").strip()
    if webui:
        return f"{site}/wiki{webui}" if webui.startswith("/") else f"{site}/wiki/{webui}"
    pid = page.get("id") or ""
    space = (page.get("space") or {}).get("key") or SPACE_DEFAULT
    return f"{site}/wiki/spaces/{space}/pages/{pid}"


def _classify(title: str, text: str) -> tuple[str, str]:
    if STUB_TITLE.search(title) or len(text) < 80:
        quality = "stub"
    elif len(text) < 700:
        quality = "thin"
    else:
        quality = "strong"
    if RUNBOOK_RE.search(title):
        kind = "runbook"
    elif ARCH_RE.search(title):
        kind = "architecture"
    elif SOP_RE.search(title) or title.lower().startswith("how"):
        kind = "sop"
    else:
        kind = "docs"
    return kind, quality


def _group_score(items: list[dict[str, Any]]) -> float | None:
    if not items:
        return None
    weights = {"strong": 1.0, "thin": 0.35, "stub": 0.10}
    ratio = sum(weights.get(i["quality"], 0.2) for i in items) / len(items)
    return round(max(0.0, min(5.0, ratio * 5)), 1)


def _hub(catalog: dict[str, Any] | None, creds: dict[str, str]) -> tuple[str, str]:
    annotations = (catalog or {}).get("annotations") or {}
    ancestor = str(annotations.get("handover.celonis.dev/confluence-ancestor") or creds["ancestor"])
    space = str(annotations.get("handover.celonis.dev/confluence-space") or creds["space"])
    return space, ancestor


def _fetch_pages(creds: dict[str, str], ancestor: str) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    start = 0
    cql = f"type=page AND (ancestor={ancestor} OR id={ancestor})"
    while start < 200:
        data = _get(
            creds,
            "/wiki/rest/api/content/search",
            {
                "cql": cql,
                "limit": 50,
                "start": start,
                "expand": "history.lastUpdated,space,body.view",
            },
        )
        batch = data.get("results") or []
        pages.extend(batch)
        if len(batch) < 50:
            break
        start += 50
    return pages


def assess_confluence(
    catalog: dict[str, Any] | None = None,
    window_days: int = 90,
) -> dict[str, Any]:
    days = min(max(int(window_days or 90), 1), 400)
    site = SITE_DEFAULT
    hub_url = f"{SITE_DEFAULT}{HUB_PATH}"
    creds = credentials()
    if not creds:
        return {
            "status": "disconnected",
            "rag": "amber",
            "title": "Not connected",
            "score": None,
            "summary": "Set CONFLUENCE_EMAIL/CONFLUENCE_TOKEN, or reuse JIRA_EMAIL/JIRA_TOKEN, to score Celospace pages.",
            "url": hub_url,
            "window_days": days,
            "signals": {},
            "why": [
                "Confluence is not connected. The same Atlassian API token as Jira works on celonis-confluence.atlassian.net."
            ],
        }

    site = creds["site"]
    space, ancestor = _hub(catalog, creds)
    hub_url = f"{site}/wiki/spaces/{space}/pages/{ancestor}"
    search_url = f"{site}/wiki/search?cql={urllib.parse.quote(f'ancestor={ancestor} AND type=page')}"
    try:
        raw_pages = _fetch_pages(creds, ancestor)
    except Exception as exc:
        return {
            "status": "error",
            "rag": "red",
            "title": "Confluence call failed",
            "score": None,
            "summary": str(exc)[:220],
            "url": hub_url,
            "window_days": days,
            "signals": {},
            "why": ["Check that the Atlassian token can read celonis-confluence.atlassian.net (space DKB)."],
        }

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    year = now - timedelta(days=365)
    pages: list[dict[str, Any]] = []
    for raw in raw_pages:
        title = str(raw.get("title") or "")
        text = _plain(((raw.get("body") or {}).get("view") or {}).get("value") or "")
        kind, quality = _classify(title, text)
        updated = _when(((raw.get("history") or {}).get("lastUpdated") or {}).get("when"))
        url = _page_url(site, raw)
        pages.append(
            {
                "id": raw.get("id"),
                "title": title,
                "kind": kind,
                "quality": quality,
                "chars": len(text),
                "updated": updated.date().isoformat() if updated else "",
                "fresh": bool(updated and updated >= cutoff),
                "stale": bool(updated and updated < year),
                "url": url,
            }
        )

    sops = [p for p in pages if p["kind"] == "sop"]
    docs = [p for p in pages if p["kind"] == "docs"]
    runbooks = [p for p in pages if p["kind"] == "runbook"]
    arch = [p for p in pages if p["kind"] == "architecture"]
    handover = [p for p in pages if "handover" in p["title"].lower()]
    onboarding = [p for p in pages if "onboarding" in p["title"].lower()]
    fresh_n = sum(1 for p in pages if p["fresh"])
    stub_n = sum(1 for p in pages if p["quality"] == "stub")
    strong_n = sum(1 for p in pages if p["quality"] == "strong")

    docs_score = _group_score(sops + docs) or 1.0
    if handover and any(p["quality"] == "strong" for p in handover):
        docs_score = min(5.0, round(docs_score + 0.4, 1))
    elif handover:
        docs_score = min(5.0, round(docs_score + 0.15, 1))
    if onboarding:
        docs_score = min(5.0, round(docs_score + 0.3, 1))
    if pages and fresh_n / len(pages) >= 0.25:
        docs_score = min(5.0, round(docs_score + 0.3, 1))
    elif pages and all(p["stale"] or not p["updated"] for p in pages):
        docs_score = max(0.0, round(docs_score - 0.5, 1))

    runbook_score = _group_score(runbooks)
    arch_score = _group_score(arch)
    # Headline is documentation quality. A single thin SLO or threat-model page
    # must not pull the live card red when SOPs are handover-grade.
    if runbook_score is not None and len(runbooks) >= 2:
        score = round(docs_score * 0.75 + runbook_score * 0.25, 1)
    else:
        score = docs_score
        if runbook_score is not None and runbook_score < 3.0:
            runbook_score = None
    if arch_score is not None and (arch_score < 3.0 or len(arch) < 2):
        arch_score = None
    rag = rag_from_score(score)

    why = [
        f"{len(pages)} Celospace page(s) under Task Mining (DKB) · {strong_n} strong · {stub_n} stub · {fresh_n} updated in {days}d.",
        f"SOPs/how-tos {len(sops)} · other docs {len(docs)} · runbook/SLO {len(runbooks)} · architecture {len(arch)} · handover {len(handover)}.",
    ]
    if rag == "red":
        why.append(f"Red because Confluence quality is {score}/5 (below 3.0). Green needs ≥4.0 — substantial, current SOPs and handover pages.")
    elif rag == "amber":
        why.append(f"Amber because Confluence quality is {score}/5. Green needs ≥4.0.")
    else:
        why.append(f"Green: Confluence quality is {score}/5.")
    if runbook_score is None:
        why.append("Few named runbook pages in the wiki — operate still leans on git runbooks + Datadog.")
    sample = next((p for p in pages if p["kind"] == "sop" and p["quality"] == "strong"), None)
    if sample:
        why.append(f"Example SOP: {sample['title']}.")

    gap = None
    if score < 3.0:
        gap = {
            "id": "confluence.docs",
            "area": "knowledge",
            "title": "Celospace documentation is thin or stale",
            "ask": "Refresh Task Mining Confluence (handover, how-tos, runbooks) so incoming is not git-only.",
            "p0": score < 2.0,
        }

    return {
        "status": "connected",
        "rag": rag,
        "title": "Connected",
        "score": score,
        "docs_score": docs_score,
        "runbook_score": runbook_score,
        "architecture_score": arch_score,
        "summary": (
            f"{len(pages)} pages · {len(sops)} SOPs · {len(runbooks)} runbook/SLO · "
            f"{fresh_n} updated in {days}d · docs {docs_score}/5."
        ),
        "url": hub_url,
        "window_days": days,
        "space": space,
        "ancestor": ancestor,
        "signals": {
            "pages": len(pages),
            "sops": len(sops),
            "runbooks": len(runbooks),
            "architecture": len(arch),
            "handover": len(handover),
            "updated": fresh_n,
            "strong": strong_n,
            "stubs": stub_n,
        },
        "links": {
            "hub": hub_url,
            "pages": search_url,
            "sops": search_url,
            "runbooks": f"{site}/wiki/search?text={urllib.parse.quote('runbook SLO Task Mining')}",
            "handover": f"{site}/wiki/spaces/{space}/pages/1346339006/Handover+documents",
            "updated": search_url,
        },
        "pages": [
            {k: p[k] for k in ("id", "title", "kind", "quality", "chars", "updated", "url")}
            for p in sorted(pages, key=lambda x: x["title"])[:40]
        ],
        "why": why[:6],
        "gap": gap,
    }


def _blend_area(
    area: dict[str, Any],
    live_score: float,
    label: str,
    *,
    git_key: str,
    live_key: str,
    extra: dict[str, float] | None = None,
) -> None:
    extra = extra or {}
    git_score = float(area.get(git_key) if area.get(git_key) is not None else area.get("score") or 0)
    if extra:
        # e.g. Drive already on knowledge: 40% git + 30% drive + 30% confluence
        keys = list(extra.items())
        if len(keys) == 1:
            other_w, other_s = 0.30, keys[0][1]
            mixed = round(git_score * 0.40 + other_s * other_w + live_score * 0.30, 1)
        else:
            mixed = round(git_score * 0.40 + live_score * 0.30 + sum(v for _, v in keys) / len(keys) * 0.30, 1)
    else:
        mixed = round(git_score * 0.55 + live_score * 0.45, 1)
    area[git_key] = git_score
    area[live_key] = live_score
    area["score"] = mixed
    area["score_pct"] = int(round((mixed / 5) * 100))
    area["rag"] = rag_from_score(mixed)
    bits = [f"Git {git_score}/5"]
    for name, val in extra.items():
        bits.append(f"{name} {val}/5")
    bits.append(f"{label} {live_score}/5")
    area["why"] = " · ".join(bits) + "."
    checks = list(area.get("checks") or [])
    status = "found" if live_score >= 4.0 else ("partial" if live_score >= 3.0 else "missing")
    checks.append(
        {
            "id": f"{area.get('id')}.confluence",
            "area": area.get("id"),
            "weight": 0,
            "status": status,
            "score": live_score / 5,
            "evidence": f"Confluence {label}",
            "files": [],
            "note": f"{live_score}/5",
        }
    )
    area["checks"] = checks


def apply_confluence(report: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    if live.get("status") != "connected" or live.get("score") is None:
        limits = list(report.get("limits") or [])
        if live.get("status") == "disconnected":
            limits.insert(0, "Confluence is not connected; Documentation/SOP quality is git + Drive only.")
        report["limits"] = limits
        report["confluence"] = live
        return report

    docs_score = float(live.get("docs_score") if live.get("docs_score") is not None else live["score"])
    runbook_score = live.get("runbook_score")
    arch_score = live.get("architecture_score")

    for area in report.get("areas") or []:
        aid = area.get("id")
        if aid == "knowledge":
            extra = {}
            if area.get("drive_score") is not None:
                extra["Drive"] = float(area["drive_score"])
            _blend_area(area, docs_score, "Confluence docs", git_key="git_score", live_key="confluence_score", extra=extra)
        elif aid == "operate" and runbook_score is not None:
            extra = {}
            if area.get("runbook_quality") is not None:
                extra["git runbooks"] = float(area["runbook_quality"])
            _blend_area(
                area,
                float(runbook_score),
                "Confluence runbooks",
                git_key="git_score",
                live_key="confluence_runbook_score",
                extra=extra,
            )
        elif aid == "architecture" and arch_score is not None:
            _blend_area(
                area,
                float(arch_score),
                "Confluence architecture",
                git_key="git_score",
                live_key="confluence_arch_score",
            )

    gap = live.get("gap")
    if gap:
        ids = {g.get("id") for g in report.get("gaps") or []}
        if gap.get("id") not in ids:
            report.setdefault("gaps", []).append(gap)

    recompute_overall(report)
    limits = [x for x in (report.get("limits") or []) if "Confluence" not in x]
    limits.insert(
        0,
        "Documentation quality includes Celospace (Task Mining hub in DKB): SOPs/how-tos blend into Knowledge, "
        "named runbook/SLO pages into Operate, architecture pages into Architecture.",
    )
    report["limits"] = limits
    report["confluence"] = live
    return report
