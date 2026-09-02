from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from scanner.env import load_dotenv_files
from scanner.score import rag_from_score

# Catalog / Backstage name → Datadog monitor_tags (from onboarding-readiness projects.canonical.json)
SERVICE_TAGS = {
    "task-mining": "service:task-mining",
    "cloud-task-mining": "service:task-mining",
    "task-mining-ai": "service:task-mining-ai",
    "task-mining-uploader": "service:task-mining-uploader",
    "tm-image-collector": "service:tm-image-collector",
    "ems-frontend_task-mining": "service:ems-frontend_task-mining",
}


def service_tag(catalog: dict[str, Any]) -> str:
    anns = catalog.get("annotations") or {}
    selector = str(anns.get("argocd/app-selector") or "")
    name = ""
    if selector.startswith("app="):
        name = selector.split("=", 1)[1].strip()
    if not name:
        name = str(catalog.get("name") or "").strip().split("/")[-1]
    if not name:
        return ""
    if name in SERVICE_TAGS:
        return SERVICE_TAGS[name]
    if name.startswith("service:"):
        return name
    return f"service:{name}"


def _is_access_token(value: str | None) -> bool:
    raw = (value or "").strip()
    return raw.startswith("ddpat_") or raw.startswith("ddsat_")


def _api_base() -> str:
    raw = os.environ.get("DD_SITE") or os.environ.get("DATADOG_SITE") or "https://api.datadoghq.com"
    raw = raw.strip().rstrip("/")
    if raw in {"datadoghq.com", "datadoghq.eu"}:
        return f"https://api.{raw}"
    if raw.startswith("http"):
        return raw
    if raw.startswith("api."):
        return f"https://{raw}"
    return f"https://api.{raw}"


def credentials() -> dict[str, str] | None:
    load_dotenv_files()
    explicit = (
        os.environ.get("DD_ACCESS_TOKEN")
        or os.environ.get("DD_PAT")
        or os.environ.get("DATADOG_ACCESS_TOKEN")
        or os.environ.get("DD_SERVICE_ACCESS_TOKEN")
        or ""
    ).strip()
    api_key = (os.environ.get("DD_API_KEY") or os.environ.get("DATADOG_API_KEY") or "").strip()
    app_key = (os.environ.get("DD_APP_KEY") or os.environ.get("DATADOG_APP_KEY") or "").strip()
    token = ""
    for candidate in (explicit, app_key, api_key):
        if _is_access_token(candidate):
            token = candidate
            break
    timeout = str(float(os.environ.get("HTTP_TIMEOUT", "45")))
    base = _api_base()
    if token:
        return {"auth": "bearer", "token": token, "api_base": base, "timeout": timeout}
    if api_key and app_key:
        return {
            "auth": "keys",
            "api_key": api_key,
            "app_key": app_key,
            "api_base": base,
            "timeout": timeout,
        }
    return None


def app_base(api_base: str) -> str:
    host = (api_base or "").split("://", 1)[-1].strip("/")
    if host.startswith("api."):
        host = host[4:]
    if host in {"datadoghq.com"}:
        return "https://celonis.datadoghq.com"
    if host.startswith("datadoghq."):
        host = "app." + host
    return "https://" + host


def _headers(creds: dict[str, str]) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if creds.get("auth") == "bearer":
        headers["Authorization"] = f"Bearer {creds['token']}"
        return headers
    headers["DD-API-KEY"] = creds["api_key"]
    headers["DD-APPLICATION-KEY"] = creds["app_key"]
    return headers


def _get(creds: dict[str, str], path: str, params: dict[str, Any] | None = None) -> Any:
    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"{creds['api_base']}{path}"
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
        raise RuntimeError(f"Datadog HTTP {exc.code} on {path}: {err_body}") from exc


def _rating_why(
    *,
    score: float,
    rag: str,
    monitors: list[dict[str, Any]],
    alert_only: list[dict[str, Any]],
    warn_n: int,
    muted: int,
    slos: list[dict[str, Any]],
    budget_low: list[dict[str, Any]],
    incidents: list[dict[str, Any]],
    sev1: list[dict[str, Any]],
    window_label: str = "90 days",
) -> list[str]:
    """Plain-language score drivers. Green is ≥4.0, amber 3.0–3.9, red <3.0."""
    why: list[str] = []
    if rag == "red":
        why.append(
            f"Red because live operate is {score}/5 (below 3.0). Incoming would inherit this pager. Green needs ≥4.0."
        )
    elif rag == "amber":
        why.append(f"Amber because live operate is {score}/5. Green needs ≥4.0.")
    else:
        why.append(f"Green: live operate is {score}/5.")

    if monitors:
        why.append(f"Monitor coverage itself is fine ({len(monitors)} monitors).")
    else:
        why.append("No monitors found for this service tag (−1.2).")

    if alert_only:
        names = ", ".join((m.get("name") or str(m.get("id") or "monitor")) for m in alert_only[:3])
        more = f" +{len(alert_only) - 3} more" if len(alert_only) > 3 else ""
        penalty = min(1.5, 0.35 * len(alert_only))
        why.append(f"{len(alert_only)} currently in Alert (−{penalty:.1f}): {names}{more}.")
    if warn_n:
        why.append(f"{warn_n} in Warn (−{min(0.6, 0.1 * warn_n):.1f}).")
    if muted > 3:
        why.append(f"{muted} muted monitors (−0.3) — coverage that cannot fire.")

    if not slos:
        why.append("No SLOs — missing the +0.5 reliability credit and any error-budget signal.")
    elif budget_low:
        why.append("SLO error budget below 10% (−1.0). Freeze risky releases.")
    else:
        why.append(f"{len(slos)} SLO(s) defined.")

    if sev1:
        raw = 0.4 * len(sev1)
        penalty = min(1.2, raw)
        cap = ", capped" if raw > 1.2 else ""
        why.append(
            f"{len(sev1)} Sev-1 incidents in {window_label} (−{penalty:.1f}{cap}). High recent severity for a handover."
        )
    elif incidents:
        why.append(f"{len(incidents)} incidents in {window_label}, none Sev-1.")
    return why[:7]


def assess_datadog(catalog: dict[str, Any], window_days: int = 90) -> dict[str, Any]:
    service = service_tag(catalog)
    creds = credentials()
    if not creds:
        return {
            "status": "disconnected",
            "rag": "amber",
            "title": "Not connected",
            "summary": "Set DD_ACCESS_TOKEN (Personal or Service Access Token) or legacy DD_API_KEY + DD_APP_KEY.",
            "service": service,
            "score": None,
            "signals": {},
            "monitors": [],
            "slos": [],
            "incidents": [],
            "setup": [
                "Create a token at Personal Settings → Access Tokens (https://celonis.datadoghq.com/personal-settings/access-tokens).",
                "Copy the secret once (starts with ddpat_ or ddsat_). Put it in handover-dashboard/.env as DD_ACCESS_TOKEN.",
                "Scopes needed: monitors_read, slos_read, incident_read.",
            ],
            "gap": {
                "id": "datadog.disconnected",
                "area": "operate",
                "title": "Datadog not connected — operate score is git-only",
                "ask": "Connect Datadog so handover scoring includes live alerts, SLO budget, and recent incidents — not just runbook files.",
                "p0": str(catalog.get("type") or "").lower() == "service",
            },
        }

    if not service:
        return {
            "status": "skipped",
            "rag": "amber",
            "title": "No service tag",
            "summary": "catalog-info has no component name / argocd app selector to query Datadog with.",
            "service": "",
            "score": None,
            "signals": {},
            "monitors": [],
            "slos": [],
            "incidents": [],
            "gap": None,
        }

    errors: list[str] = []
    monitors: list[dict[str, Any]] = []
    slos: list[dict[str, Any]] = []
    incidents: list[dict[str, Any]] = []

    try:
        monitors = _fetch_monitors(creds, service)
    except Exception as exc:
        errors.append(str(exc))
    try:
        slos = _fetch_slos(creds, service)
    except Exception as exc:
        errors.append(str(exc))
    try:
        incidents = _fetch_incidents(creds, service, window_days)
    except Exception as exc:
        errors.append(str(exc))

    if errors and not monitors and not slos and not incidents:
        return {
            "status": "error",
            "rag": "red",
            "title": "Datadog call failed",
            "summary": errors[0][:300],
            "service": service,
            "score": None,
            "signals": {},
            "monitors": [],
            "slos": [],
            "incidents": [],
            "errors": errors,
            "gap": {
                "id": "datadog.error",
                "area": "operate",
                "title": "Datadog API failed",
                "ask": "Fix Datadog auth: use a Personal/Service Access Token (DD_ACCESS_TOKEN) or valid API+app keys. Site is DD_SITE=https://api.datadoghq.com.",
                "p0": False,
            },
        }

    alerting = [m for m in monitors if m.get("status") in {"Alert", "Warn"}]
    alert_only = [m for m in monitors if m.get("status") == "Alert"]
    ok_monitors = [m for m in monitors if m.get("status") in {"OK", "Ignored"}]
    muted = sum(1 for m in monitors if m.get("muted"))
    sev1 = [i for i in incidents if i.get("severity") in {"SEV-1", "SEV1", "1"}]
    budget_low = [s for s in slos if s.get("budget_remaining") is not None and s["budget_remaining"] < 10]
    slo_ok = [s for s in slos if s.get("status") in {"ok", "OK"}]

    score = 3.0
    if monitors:
        score += 0.8 if len(monitors) >= 5 else 0.4
    elif str(catalog.get("type") or "").lower() == "service":
        score -= 1.2
    score -= min(1.5, 0.35 * len(alert_only))
    score -= min(0.6, 0.1 * len([m for m in alerting if m.get("status") == "Warn"]))
    if muted > 3:
        score -= 0.3
    if slos:
        score += 0.5 if slo_ok else 0.2
        if budget_low:
            score -= 1.0
    if sev1:
        score -= min(1.2, 0.4 * len(sev1))
    elif incidents:
        score += 0.15  # team has exercised incident process
    score = max(0.0, min(5.0, round(score, 1)))
    rag = rag_from_score(score)

    win_days = int(window_days or 90)
    if win_days <= 0:
        win_days = 365
    win_days = min(win_days, 400)
    win_label = f"{win_days} days"
    parts = [
        f"{len(monitors)} monitors ({len(alert_only)} Alert, {muted} muted)",
        f"{len(slos)} SLOs",
        f"{len(incidents)} incidents in {win_label} ({len(sev1)} Sev-1)",
    ]
    summary = "; ".join(parts) + f". Live operate score {score}/5."
    if errors:
        summary += " Partial: " + errors[0][:160]
    why = _rating_why(
        score=score,
        rag=rag,
        monitors=monitors,
        alert_only=alert_only,
        warn_n=len([m for m in alerting if m.get("status") == "Warn"]),
        muted=muted,
        slos=slos,
        budget_low=budget_low,
        incidents=incidents,
        sev1=sev1,
        window_label=win_label,
    )

    gap = None
    if alert_only:
        gap = {
            "id": "datadog.alerting",
            "area": "operate",
            "title": f"{len(alert_only)} Datadog monitor(s) currently in Alert",
            "ask": "New team would inherit a burning pager. Restore green or document accepted risk before primary on-call moves.",
            "p0": True,
        }
    elif budget_low:
        gap = {
            "id": "datadog.slo_budget",
            "area": "operate",
            "title": "SLO error budget below 10%",
            "ask": "Error budget is nearly exhausted. Freeze risky releases and fund reliability work as part of handover.",
            "p0": True,
        }
    elif not monitors and str(catalog.get("type") or "").lower() == "service":
        gap = {
            "id": "datadog.no_monitors",
            "area": "operate",
            "title": f"No Datadog monitors found for {service}",
            "ask": "Confirm the service tag and monitor coverage. A cloud service with no monitors is not handover-ready.",
            "p0": True,
        }

    ui = app_base(creds["api_base"])
    tag_q = urllib.parse.quote(f'tag:"{service}"')
    alert_q = urllib.parse.quote(f'status:Alert tag:"{service}"')
    return {
        "status": "connected",
        "rag": rag,
        "title": "Connected (access token)" if creds.get("auth") == "bearer" else "Connected",
        "summary": summary,
        "service": service,
        "score": score,
        "site": creds["api_base"],
        "signals": {
            "monitors": len(monitors),
            "alerting": len(alert_only),
            "alerts": len(alert_only),
            "p0": len(sev1),
            "warn": len([m for m in alerting if m.get("status") == "Warn"]),
            "muted": muted,
            "ok": len(ok_monitors),
            "slos": len(slos),
            "budget_low": len(budget_low),
            "incidents_90d": len(incidents),
            "sev1_90d": len(sev1),
            "window_days": win_days,
        },
        "window_days": win_days,
        "monitors": monitors[:25],
        "slos": slos[:15],
        "incidents": incidents[:15],
        "errors": errors,
        "why": why,
        "gap": gap,
        "links": {
            "monitors": f"{ui}/monitors/manage?q={tag_q}",
            "alerting": f"{ui}/monitors/manage?q={alert_q}",
            "alerts": f"{ui}/monitors/manage?q={alert_q}",
            "p0": f"{ui}/incidents?query={urllib.parse.quote(f'severity:SEV-1 {service}')}",
            "apm": f"{ui}/apm/traces?query={urllib.parse.quote(service)}",
            "logs": f"{ui}/logs?query={urllib.parse.quote(service + ' -@logType:Client')}",
            "incidents": f"{ui}/incidents?query={urllib.parse.quote(service)}",
        },
    }


def _fetch_monitors(creds: dict[str, str], service: str) -> list[dict[str, Any]]:
    """Same as onboarding-readiness: GET /api/v1/monitor?monitor_tags=<tag>."""
    data = _get(creds, "/api/v1/monitor", {"monitor_tags": service, "page_size": 1000})
    monitors = data if isinstance(data, list) else (data.get("monitors") or [])
    ui = app_base(creds["api_base"])
    out = []
    for mon in monitors:
        muted = bool((mon.get("options") or {}).get("silenced"))
        out.append(
            {
                "id": mon.get("id"),
                "name": mon.get("name") or mon.get("title") or "",
                "status": mon.get("overall_state") or mon.get("status") or "",
                "type": mon.get("type") or "",
                "muted": muted,
                "url": f"{ui}/monitors/{mon.get('id')}" if mon.get("id") else "",
            }
        )
    return out


def _fetch_slos(creds: dict[str, str], service: str) -> list[dict[str, Any]]:
    data = _get(creds, "/api/v1/slo", {"query": service, "limit": 30})
    raw = data.get("data") or [] if isinstance(data, dict) else []
    out = []
    for slo in raw:
        overall = (slo.get("overall") or {}) if isinstance(slo, dict) else {}
        remaining = overall.get("remaining")
        try:
            remaining_pct = float(remaining) if remaining is not None else None
        except (TypeError, ValueError):
            remaining_pct = None
        out.append(
            {
                "id": slo.get("id"),
                "name": slo.get("name") or "",
                "status": (overall.get("status") or slo.get("status") or ""),
                "budget_remaining": remaining_pct,
                "target": (slo.get("thresholds") or [{}])[0].get("target") if slo.get("thresholds") else slo.get("target_threshold"),
            }
        )
    return out


def _parse_incident(inc: dict[str, Any]) -> dict[str, Any]:
    inner = inc.get("data") if isinstance(inc.get("data"), dict) else inc
    attrs = inner.get("attributes") or {}
    fields = attrs.get("fields") or {}
    severity = ""
    sev_obj = fields.get("severity") or {}
    if isinstance(sev_obj, dict):
        severity = str(sev_obj.get("value") or "")
    iid = inner.get("id")
    return {
        "id": iid,
        "title": attrs.get("title") or "",
        "state": attrs.get("state") or "",
        "severity": severity,
        "created": attrs.get("created") or "",
        "url": f"https://celonis.datadoghq.com/incidents/{iid}" if iid else "",
    }


def _created_since(created: str, cutoff: datetime) -> bool:
    if not created:
        return False
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= cutoff


def _fetch_incidents(creds: dict[str, str], service: str, window_days: int = 90) -> list[dict[str, Any]]:
    days = int(window_days or 90)
    if days <= 0:
        days = 365
    days = min(days, 400)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    since_unix = int(cutoff.timestamp())
    svc = service.split(":", 1)[-1] if service else ""
    query = f"services:{svc} created_after:{since_unix}" if svc else f"created_after:{since_unix}"
    raw: list[dict[str, Any]] = []
    try:
        data = _get(
            creds,
            "/api/v2/incidents/search",
            {"query": query, "sort": "-created", "page[size]": 100},
        )
        raw = ((data.get("data") or {}).get("attributes") or {}).get("incidents") or []
    except Exception:
        listed = _get(creds, "/api/v2/incidents", {"page[size]": 100})
        raw = listed.get("data") or []
    out = []
    for inc in raw:
        if not isinstance(inc, dict):
            continue
        row = _parse_incident(inc)
        if _created_since(row.get("created") or "", cutoff):
            out.append(row)
    return out


def apply_datadog(report: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    from scanner.score import decision

    gap = live.get("gap")
    if gap:
        ids = {g.get("id") for g in report.get("gaps") or []}
        if gap.get("id") not in ids:
            report.setdefault("gaps", []).append(gap)
        if gap.get("p0") and live.get("status") in {"connected", "disconnected"}:
            if live.get("rag") == "red" or live.get("status") == "disconnected":
                if "operate" not in (report.get("p0_reds") or []) and live.get("status") == "connected":
                    report.setdefault("p0_reds", []).append("operate")
                elif live.get("status") == "disconnected" and gap.get("p0"):
                    report.setdefault("p0_ambers", []).append("datadog")
            elif live.get("rag") == "amber" and gap.get("p0"):
                report.setdefault("p0_ambers", []).append("datadog")

    if live.get("status") == "connected" and live.get("score") is not None:
        for area in report.get("areas") or []:
            if area.get("id") != "operate":
                continue
            git_score = float(area.get("score") or 0)
            mixed = round(git_score * 0.55 + float(live["score"]) * 0.45, 1)
            area["git_score"] = git_score
            area["datadog_score"] = live["score"]
            area["score"] = mixed
            area["score_pct"] = int(round((mixed / 5) * 100))
            area["rag"] = rag_from_score(mixed)
            area["why"] = f"Git {git_score}/5 blended with Datadog {live['score']}/5."
        # recompute overall
        weighted = 0.0
        total = 0.0
        p0_reds = [x for x in (report.get("p0_reds") or []) if x != "operate"]
        p0_ambers = [x for x in (report.get("p0_ambers") or []) if x != "operate"]
        for area in report.get("areas") or []:
            w = float(area.get("weight") or 0)
            s = float(area.get("score") or 0)
            weighted += s * w
            total += w
            if area.get("p0"):
                if area.get("rag") == "red":
                    p0_reds.append(area["id"])
                elif area.get("rag") == "amber":
                    p0_ambers.append(area["id"])
        overall_0_5 = weighted / total if total else 0
        report["overall_score"] = round(overall_0_5, 2)
        report["overall_pct"] = round((overall_0_5 / 5) * 100)
        report["p0_reds"] = list(dict.fromkeys(p0_reds))
        report["p0_ambers"] = list(dict.fromkeys(p0_ambers))
        if live.get("gap") and live["gap"].get("p0") and live.get("rag") == "red":
            if "operate" not in report["p0_reds"]:
                report["p0_reds"].append("operate")

    limits = [x for x in (report.get("limits") or []) if "Datadog/Argo" not in x]
    if live.get("status") == "connected":
        limits.insert(0, "Operate score is 55% git runbooks + 45% live Datadog (monitors, SLOs, incidents in the selected window).")
    else:
        limits.insert(0, "Datadog is not connected in this scan; operate score is git-only.")
    report["limits"] = limits

    verdict = decision(report["overall_pct"], report.get("p0_reds") or [], report.get("p0_ambers") or [])
    report["verdict"] = verdict
    report["overall_rag"] = {"block": "red", "conditional": "amber", "go": "green"}[verdict["verdict"]]
    report["datadog"] = live
    return report
