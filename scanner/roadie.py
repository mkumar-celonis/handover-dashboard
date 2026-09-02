from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from scanner.env import load_dotenv_files
from scanner.score import rag_from_score, recompute_overall

APP_BASE = "https://celonis.roadie.so"
API_BASE = "https://api.roadie.so/api"

AREA_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ownership": ("owner", "ownership", "on-call", "oncall", "team", "codeowners"),
    "architecture": ("architecture", "runtime", "dependenc", "system design"),
    "operate": (
        "operat",
        "incident",
        "sre",
        "production",
        "observab",
        "alert",
        "monitor",
        "readiness",
        "slo",
    ),
    "security": ("secur", "cve", "snyk", "compliance", "secret", "vulnerab"),
    "release": ("release", "deploy", "pipeline", "ci/cd", "cicd", "change", "kargo"),
    "data": ("data", "backup", "persist", "schema", "retention"),
    "product": ("api", "contract", "sla", "product", "customer"),
    "knowledge": ("doc", "techdocs", "readme", "knowledge", "adr", "runbook"),
}


def credentials() -> dict[str, str] | None:
    load_dotenv_files()
    token = (
        os.environ.get("ROADIE_API_TOKEN")
        or os.environ.get("ROADIE_TOKEN")
        or os.environ.get("BACKSTAGE_TOKEN")
        or ""
    ).strip()
    if not token:
        return None
    api_base = (os.environ.get("ROADIE_API_BASE") or API_BASE).rstrip("/")
    app_base = (os.environ.get("ROADIE_APP_BASE") or APP_BASE).rstrip("/")
    timeout = os.environ.get("HTTP_TIMEOUT", "45")
    return {"token": token, "api_base": api_base, "app_base": app_base, "timeout": timeout}


def entity_ref(catalog: dict[str, Any]) -> str:
    name = str(catalog.get("name") or "").strip()
    if not name:
        return "component:default/task-mining"
    if name.startswith("component:"):
        return name
    if "/" in name and not name.startswith("component:"):
        return f"component:{name}" if name.count("/") == 1 else name
    return f"component:default/{name}"


def entity_url(ref: str, app_base: str) -> str:
    kind, _, rest = ref.partition(":")
    namespace, _, name = rest.partition("/")
    if not name:
        namespace, name = "default", rest
    return f"{app_base}/catalog/{namespace}/{kind}/{name}/scorecards"


def map_area(title: str) -> str | None:
    text = (title or "").lower()
    for area, keys in AREA_KEYWORDS.items():
        if any(key in text for key in keys):
            return area
    return None


def _get(creds: dict[str, str], url: str, body: dict[str, Any] | None = None) -> Any:
    headers = {
        "Authorization": f"Bearer {creds['token']}",
        "Accept": "application/json",
    }
    data = None
    method = "GET"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    timeout = float(creds.get("timeout") or 45)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Roadie HTTP {exc.code} on {url}: {err}") from exc


def _try_get(creds: dict[str, str], url: str, body: dict[str, Any] | None = None) -> Any | None:
    try:
        return _get(creds, url, body)
    except Exception:
        return None


def _as_list(data: Any) -> list[Any]:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("scorecards", "results", "checks", "items", "data"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    return []


def _scorecard_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("identifier") or item.get("uid") or item.get("name") or "")


def _scorecard_title(item: dict[str, Any]) -> str:
    return str(item.get("title") or item.get("name") or item.get("id") or "Scorecard")


def _match_entity(row: dict[str, Any], ref: str) -> bool:
    candidates = [
        str(row.get("entity") or ""),
        str(row.get("entityRef") or ""),
        str((row.get("entity") or {}).get("ref") if isinstance(row.get("entity"), dict) else ""),
    ]
    want = {ref.lower(), ref.split(":", 1)[-1].lower()}
    return any(c.lower() in want or c.lower().endswith("/" + ref.split("/")[-1].lower()) for c in candidates if c)


def _pass_fail(row: dict[str, Any]) -> tuple[int, int]:
    success = row.get("success")
    failing = row.get("failing")
    if success is None:
        success = row.get("passed") or row.get("passing") or row.get("successCount")
    if failing is None:
        failing = row.get("failed") or row.get("failingCount") or row.get("failures")
    try:
        ok = int(success) if success is not None else 0
        bad = int(failing) if failing is not None else 0
    except (TypeError, ValueError):
        ok, bad = 0, 0
    if ok or bad:
        return ok, bad
    total = row.get("total") or row.get("checksCount")
    percent = row.get("percentage") or row.get("score") or row.get("percent")
    try:
        if total is not None and percent is not None:
            tot = int(total)
            pct = float(percent)
            if pct <= 1:
                pct *= 100
            ok = int(round(tot * pct / 100))
            return ok, max(0, tot - ok)
    except (TypeError, ValueError):
        pass
    return 0, 0


def _pct_to_score(ok: int, bad: int) -> float:
    total = ok + bad
    if total <= 0:
        return 0.0
    return round((ok / total) * 5, 1)


def _list_scorecards(creds: dict[str, str]) -> list[dict[str, Any]]:
    bases = [creds["api_base"], f"{creds['app_base']}/api"]
    paths = [
        "/tech-insights/v1/scorecards",
        "/tech-insights/scorecards",
    ]
    for base in bases:
        for path in paths:
            data = _try_get(creds, f"{base}{path}")
            items = [x for x in _as_list(data) if isinstance(x, dict)]
            if items:
                return items
            if isinstance(data, list) and data:
                return [x for x in data if isinstance(x, dict)]
    return []


def _entity_result(creds: dict[str, str], scorecard_id: str, ref: str) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(scorecard_id, safe="")
    ref_q = urllib.parse.quote(ref, safe="")
    bases = [creds["api_base"], f"{creds['app_base']}/api"]
    urls = []
    for base in bases:
        urls.extend(
            [
                f"{base}/tech-insights/scorecards/entity-results/{encoded}",
                f"{base}/tech-insights/scorecards/{encoded}/entity-results",
                f"{base}/tech-insights/v1/scorecards/{encoded}/entity-results",
                f"{base}/tech-insights/scorecards/{encoded}?entity={ref_q}",
            ]
        )
    for url in urls:
        data = _try_get(creds, url)
        if data is None:
            continue
        rows = _as_list(data) or ([data] if isinstance(data, dict) else [])
        for row in rows:
            if isinstance(row, dict) and _match_entity(row, ref):
                return row
        if isinstance(data, dict) and not rows:
            ok, bad = _pass_fail(data)
            if ok or bad:
                return data
    return None


def _run_checks(creds: dict[str, str], ref: str, check_ids: list[str]) -> list[dict[str, Any]]:
    body: dict[str, Any] = {"entities": [ref]}
    if check_ids:
        body["checks"] = check_ids
    bases = [creds["api_base"], f"{creds['app_base']}/api"]
    for base in bases:
        data = _try_get(creds, f"{base}/tech-insights/v1/checks/run", body)
        if data is not None:
            return [x for x in _as_list(data) if isinstance(x, dict)]
        data = _try_get(creds, f"{base}/tech-insights/checks/run", body)
        if data is not None:
            return [x for x in _as_list(data) if isinstance(x, dict)]
    return []


def _check_passed(row: dict[str, Any]) -> bool | None:
    if "result" in row:
        val = row["result"]
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in {"true", "pass", "passed", "success"}
    for key in ("passed", "success", "isPassing"):
        if key in row:
            return bool(row[key])
    status = str(row.get("status") or row.get("state") or "").lower()
    if status in {"pass", "passed", "success", "ok"}:
        return True
    if status in {"fail", "failed", "error"}:
        return False
    return None


def _rating_why(
    avg: float | None,
    rag: str,
    scorecards: list[dict[str, Any]],
    failed_n: int,
    passed_n: int,
) -> list[str]:
    """Plain-language score drivers. Green is ≥4.0 (~80% checks passing)."""
    why: list[str] = []
    if avg is None:
        why.append("No pass/fail counts, so this cannot be scored green.")
        return why
    if rag == "red":
        why.append(f"Red because catalog checks average {avg}/5 (below 3.0). Green needs ≥4.0 (~80% passing).")
    elif rag == "amber":
        why.append(f"Amber because catalog checks average {avg}/5. Green needs ≥4.0 (~80% passing).")
    else:
        why.append(f"Green: catalog checks average {avg}/5.")

    total = passed_n + failed_n
    if total:
        pct = round((passed_n / total) * 100)
        why.append(f"{passed_n}/{total} checks passing ({pct}%). {failed_n} still failing.")

    weak = sorted(
        [c for c in scorecards if c.get("score") is not None and (int(c.get("failed") or 0) > 0 or float(c.get("score") or 5) < 4.0)],
        key=lambda c: float(c.get("score") or 0),
    )
    if not weak:
        weak = sorted(
            [c for c in scorecards if c.get("score") is not None],
            key=lambda c: float(c.get("score") or 0),
        )[:3]
    for card in weak[:4]:
        title = card.get("title") or card.get("id") or "Scorecard"
        why.append(
            f"{title}: {card.get('passed', 0)} pass / {card.get('failed', 0)} fail → {card.get('score')}/5."
        )
    return why[:6]


def assess_roadie(catalog: dict[str, Any]) -> dict[str, Any]:
    ref = entity_ref(catalog)
    creds = credentials()
    url = entity_url(ref, APP_BASE)
    if not creds:
        return {
            "status": "disconnected",
            "rag": "amber",
            "title": "Not connected",
            "summary": "Set ROADIE_API_TOKEN to pull live scorecards from celonis.roadie.so.",
            "entity": ref,
            "url": url,
            "score": None,
            "scorecards": [],
            "setup": [
                "Create a User Token at https://celonis.roadie.so/administration (Roadie API Access).",
                "Put ROADIE_API_TOKEN=... in handover-dashboard/.env",
            ],
            "gap": {
                "id": "roadie.disconnected",
                "area": "knowledge",
                "title": "Roadie scorecards not connected",
                "ask": "Connect Roadie so handover scoring includes catalog Tech Insights, not just git files.",
                "p0": False,
            },
        }

    errors: list[str] = []
    try:
        cards = _list_scorecards(creds)
    except Exception as exc:
        cards = []
        errors.append(str(exc))

    scorecards: list[dict[str, Any]] = []
    if cards:
        for item in cards:
            sid = _scorecard_id(item)
            title = _scorecard_title(item)
            row = _entity_result(creds, sid, ref) if sid else None
            ok, bad = _pass_fail(row or {})
            if not ok and not bad:
                check_ids = []
                for chk in item.get("checks") or []:
                    if isinstance(chk, dict) and chk.get("id"):
                        check_ids.append(str(chk["id"]))
                    elif isinstance(chk, str):
                        check_ids.append(chk)
                runs = _run_checks(creds, ref, check_ids)
                for run in runs:
                    passed = _check_passed(run)
                    if passed is True:
                        ok += 1
                    elif passed is False:
                        bad += 1
            total = ok + bad
            pct = round((ok / total) * 100) if total else None
            score = _pct_to_score(ok, bad) if total else None
            scorecards.append(
                {
                    "id": sid,
                    "title": title,
                    "passed": ok,
                    "failed": bad,
                    "percent": pct,
                    "score": score,
                    "rag": rag_from_score(score) if score is not None else "amber",
                    "area": map_area(title),
                    "url": url,
                }
            )
    else:
        runs = _run_checks(creds, ref, [])
        if runs:
            ok = sum(1 for r in runs if _check_passed(r) is True)
            bad = sum(1 for r in runs if _check_passed(r) is False)
            total = ok + bad
            pct = round((ok / total) * 100) if total else None
            score = _pct_to_score(ok, bad) if total else None
            scorecards.append(
                {
                    "id": "checks",
                    "title": "Tech Insights checks",
                    "passed": ok,
                    "failed": bad,
                    "percent": pct,
                    "score": score,
                    "rag": rag_from_score(score) if score is not None else "amber",
                    "area": None,
                    "url": url,
                }
            )
        elif errors:
            return {
                "status": "error",
                "rag": "red",
                "title": "Roadie call failed",
                "summary": errors[0][:300],
                "entity": ref,
                "url": url,
                "score": None,
                "scorecards": [],
                "errors": errors,
                "gap": {
                    "id": "roadie.error",
                    "area": "knowledge",
                    "title": "Roadie API failed",
                    "ask": "Check ROADIE_API_TOKEN at https://celonis.roadie.so/administration (User Token / Service Token).",
                    "p0": False,
                },
            }

    scored = [c for c in scorecards if c.get("score") is not None]
    avg = round(sum(float(c["score"]) for c in scored) / len(scored), 1) if scored else None
    failed_n = sum(int(c.get("failed") or 0) for c in scorecards)
    passed_n = sum(int(c.get("passed") or 0) for c in scorecards)
    rag = rag_from_score(avg) if avg is not None else "amber"
    if avg is None and scorecards:
        summary = f"Listed {len(scorecards)} scorecard(s) but no pass/fail counts for {ref}."
        status = "partial"
    elif avg is None:
        summary = f"No scorecards returned for {ref}."
        status = "empty"
        rag = "amber"
    else:
        summary = (
            f"{len(scorecards)} Roadie scorecard(s) for {ref}: "
            f"{passed_n} checks passing, {failed_n} failing. Live catalog score {avg}/5."
        )
        status = "connected"

    why = _rating_why(avg, rag, scorecards, failed_n, passed_n)

    gap = None
    if failed_n and avg is not None and avg < 3.0:
        gap = {
            "id": "roadie.failing",
            "area": "knowledge",
            "title": f"{failed_n} Roadie scorecard check(s) failing on {ref}",
            "ask": f"Close failing checks on {url} before treating catalog maturity as handover-ready.",
            "p0": avg < 2.0,
        }

    return {
        "status": status,
        "rag": rag,
        "title": "Connected" if status == "connected" else ("No results" if status == "empty" else "Partial"),
        "summary": summary,
        "entity": ref,
        "url": url,
        "score": avg,
        "scorecards": scorecards,
        "errors": errors,
        "why": why,
        "gap": gap,
        "signals": {
            "scorecards": len(scorecards),
            "passed": passed_n,
            "failed": failed_n,
        },
    }


def apply_roadie(report: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    gap = live.get("gap")
    if gap:
        ids = {g.get("id") for g in report.get("gaps") or []}
        if gap.get("id") not in ids:
            report.setdefault("gaps", []).append(gap)

    mapped: dict[str, list[float]] = {}
    for card in live.get("scorecards") or []:
        area_id = card.get("area")
        if area_id and card.get("score") is not None:
            mapped.setdefault(area_id, []).append(float(card["score"]))

    if live.get("status") == "connected" and (mapped or live.get("score") is not None):
        for area in report.get("areas") or []:
            scores = mapped.get(area.get("id") or "")
            if not scores:
                continue
            roadie_score = round(sum(scores) / len(scores), 1)
            git_score = float(area.get("score") or 0)
            mixed = round(git_score * 0.55 + roadie_score * 0.45, 1)
            area["git_score"] = area.get("git_score", git_score)
            area["roadie_score"] = roadie_score
            area["score"] = mixed
            area["score_pct"] = int(round((mixed / 5) * 100))
            area["rag"] = rag_from_score(mixed)
            area["why"] = f"Git {git_score}/5 blended with Roadie {roadie_score}/5."
        recompute_overall(report)
        if not mapped and live.get("score") is not None:
            from scanner.score import decision

            mixed_overall = round(float(report["overall_score"]) * 0.60 + float(live["score"]) * 0.40, 2)
            report["overall_score"] = mixed_overall
            report["overall_pct"] = round((mixed_overall / 5) * 100)
            verdict = decision(report["overall_pct"], report.get("p0_reds") or [], report.get("p0_ambers") or [])
            report["verdict"] = verdict
            report["overall_rag"] = {"block": "red", "conditional": "amber", "go": "green"}[verdict["verdict"]]

        if gap and gap.get("p0") and live.get("rag") == "red":
            if "roadie" not in report.get("p0_reds", []):
                report.setdefault("p0_reds", []).append("roadie")

    limits = list(report.get("limits") or [])
    if live.get("status") == "connected":
        limits.insert(0, "Area scores that match a Roadie scorecard are 55% git + 45% catalog Tech Insights.")
    else:
        limits.insert(0, "Roadie scorecards are not connected; scores are git/Datadog only.")
    report["limits"] = limits
    report["roadie"] = live
    return report
