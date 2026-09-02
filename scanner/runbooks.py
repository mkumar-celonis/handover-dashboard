from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from scanner.files import match_glob, read_text, rel
from scanner.score import rag_from_score, recompute_overall

RUNBOOK_GLOBS = [
    "docs/runbooks/**/*.md",
    "runbooks/**/*.md",
    "docs/ops/**/*.md",
    "docs/on-call/**/*.md",
]

STUB_RE = re.compile(
    r"TODO:|FIXME:|add content|keep this section\?|this is just an example|define rules for when",
    re.IGNORECASE,
)
STEP_RE = re.compile(r"^\s*(?:>\s*)*(?:\d+\.|[-*])\s+\S+", re.MULTILINE)
ACTION_RE = re.compile(
    r"datadog|argo\s*cd|argocd|cfg-ibc|rollback|restart|opsgenie|pql|how to resolve",
    re.IGNORECASE,
)


def _classify(text: str, name: str) -> str:
    body = (text or "").strip()
    if not body:
        return "stub"
    if STUB_RE.search(body):
        return "stub"
    steps = len(STEP_RE.findall(body))
    chars = len(body)
    has_action = bool(ACTION_RE.search(body))
    if name.lower() == "index.md" and chars < 1200 and steps < 4:
        return "index"
    if chars < 700 or steps < 3:
        return "thin"
    if has_action and chars >= 700 and steps >= 3:
        return "strong"
    if chars >= 2000 and steps >= 4:
        return "strong"
    return "thin"


def _short(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 1 else path


def assess_runbooks(
    root: Path,
    files: list[Path],
    datadog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hits: list[Path] = []
    seen: set[str] = set()
    for pattern in RUNBOOK_GLOBS:
        for path in match_glob(root, pattern, files, limit=300):
            key = rel(root, path)
            if key in seen or path.suffix.lower() != ".md":
                continue
            seen.add(key)
            hits.append(path)

    buckets: dict[str, list[str]] = {"strong": [], "thin": [], "stub": [], "index": []}
    sizes: dict[str, int] = {}
    for path in hits:
        text = read_text(path)
        kind = _classify(text, path.name)
        posix = rel(root, path).replace("\\", "/")
        buckets[kind].append(posix)
        sizes[posix] = len((text or "").strip())
    buckets["thin"].sort(key=lambda p: sizes.get(p, 0))
    buckets["strong"].sort(key=lambda p: -sizes.get(p, 0))

    classified = len(buckets["strong"]) + len(buckets["thin"]) + len(buckets["stub"])
    if not hits:
        quality = 0.0
        summary = "No runbook files found."
    elif classified == 0:
        quality = 1.5
        summary = f"{len(hits)} runbook files, but only index/overview pages — no recoverable procedures."
    else:
        weighted = (
            len(buckets["strong"]) * 1.0
            + len(buckets["thin"]) * 0.35
            + len(buckets["stub"]) * 0.10
        )
        quality = round((weighted / classified) * 5, 1)
        summary = (
            f"{len(buckets['strong'])} strong · {len(buckets['thin'])} thin · "
            f"{len(buckets['stub'])} stubs"
        )

    findings: list[str] = []
    if buckets["strong"]:
        preferred = [p for p in buckets["strong"] if "availab" in p or "/prr/" in p]
        sample = _short((preferred or buckets["strong"])[0])
        findings.append(f"Some pages are handover-grade (e.g. {sample}).")
    if buckets["thin"]:
        n = len(buckets["thin"])
        names = ", ".join(_short(p) for p in buckets["thin"][:2])
        findings.append(
            f"{n} alert page{'s are' if n != 1 else ' is'} thin “investigate” notes, not 5-minute recoveries ({names})."
        )
    if buckets["stub"]:
        n = len(buckets["stub"])
        names = ", ".join(_short(p) for p in buckets["stub"][:2])
        findings.append(
            f"{n} page{'s still have' if n != 1 else ' still has'} TODO/FIXME stubs ({names})."
        )
    if classified and len(buckets["strong"]) / classified < 0.8:
        findings.append("Handover bar is runbooks for ~80% of live alerts with 5-minute steps — file count is not enough.")

    coverage = None
    monitors = int(((datadog or {}).get("signals") or {}).get("monitors") or 0)
    alert_pages = sum(
        1
        for p in buckets["strong"] + buckets["thin"] + buckets["stub"]
        if "/monitor/" in p.replace("\\", "/")
    )
    if (datadog or {}).get("status") == "connected" and monitors >= 5:
        coverage = round(min(1.0, alert_pages / monitors), 2)
        findings.append(
            f"Named monitor runbooks cover ~{int(coverage * 100)}% of live Datadog monitors "
            f"({alert_pages}/{monitors}). Target is ~80%."
        )
        if coverage < 0.8 and quality > 0:
            quality = round(quality * (0.7 + 0.3 * (coverage / 0.8)), 1)

    # Mixed catalogs are not green: stubs, thin notes, or <80% monitor coverage.
    if classified:
        strong_share = len(buckets["strong"]) / classified
        if buckets["stub"]:
            quality = min(quality, 3.4)
        if strong_share < 0.8:
            quality = min(quality, 3.6)
        if coverage is not None and coverage < 0.8:
            quality = min(quality, 3.5)
        quality = round(quality, 1)

    if not findings and hits:
        findings.append("Runbooks look actionable enough for a Sev-1 from the repo.")

    rag = rag_from_score(quality)
    title = summary if hits else "No runbooks"
    return {
        "status": "scanned",
        "title": title,
        "summary": summary,
        "score": quality,
        "rag": rag,
        "counts": {k: len(v) for k, v in buckets.items()},
        "files": len(hits),
        "classified": classified,
        "coverage": coverage,
        "monitors": monitors or None,
        "findings": findings[:5],
        "examples": {
            "strong": buckets["strong"][:3],
            "thin": buckets["thin"][:3],
            "stub": buckets["stub"][:3],
        },
        "gap": _gap(quality, rag, classified, buckets),
    }


def _gap(quality: float, rag: str, classified: int, buckets: dict[str, list[str]]) -> dict[str, Any] | None:
    if rag == "green":
        return None
    thin = len(buckets["thin"])
    stub = len(buckets["stub"])
    p0 = rag == "red" or (classified > 0 and thin + stub > len(buckets["strong"]))
    return {
        "id": "runbooks.quality",
        "area": "operate",
        "title": "Runbooks exist but are not handover-quality",
        "ask": "Replace TODO stubs and 2-line “investigate” notes with 5-minute recovery steps "
        "(Datadog query, rollback/restart, who to page). Game-day still required.",
        "p0": p0,
    }


def apply_runbooks(report: dict[str, Any], assessed: dict[str, Any]) -> dict[str, Any]:
    """Blend runbook *quality* into Operate (existence alone is not a pass)."""
    quality = float(assessed.get("score") or 0)
    for area in report.get("areas") or []:
        if area.get("id") != "operate":
            continue
        prior = float(area.get("score") or 0)
        mixed = round(prior * 0.55 + quality * 0.45, 1)
        if quality < 4.0:
            mixed = min(mixed, 3.8)
        if quality < 3.0:
            mixed = min(mixed, 3.4)
        if quality < 2.0:
            mixed = min(mixed, 2.5)
        area["git_score"] = area.get("git_score", prior)
        area["runbook_quality"] = quality
        area["score"] = mixed
        area["score_pct"] = int(round((mixed / 5) * 100))
        area["rag"] = rag_from_score(mixed)
        area["why"] = f"Runbook quality {quality}/5 blended with prior operate {prior}/5 · {assessed.get('summary') or ''}".strip()
        checks = list(area.get("checks") or [])
        status = "found" if quality >= 4.0 else ("partial" if quality >= 3.0 else "missing")
        checks.append(
            {
                "id": "operate.runbook_quality",
                "area": "operate",
                "weight": 0,
                "status": status,
                "score": quality / 5,
                "evidence": "Runbook quality (strong vs thin vs stub)",
                "files": [],
                "note": assessed.get("summary"),
            }
        )
        area["checks"] = checks

    gap = assessed.get("gap")
    if gap:
        ids = {g.get("id") for g in report.get("gaps") or []}
        if gap.get("id") not in ids:
            report.setdefault("gaps", []).append(gap)

    recompute_overall(report)
    if gap and gap.get("p0") and assessed.get("rag") == "red":
        if "operate" not in (report.get("p0_reds") or []):
            report.setdefault("p0_reds", []).append("operate")

    limits = [x for x in (report.get("limits") or []) if "runbook quality" not in x.lower()]
    limits.insert(
        0,
        "Operate is 55% prior score (git + Datadog/Roadie) + 45% runbook quality "
        "(stubs/thin notes vs 5-minute recoveries; ~80% of live monitors).",
    )
    report["limits"] = limits
    report["runbooks"] = assessed
    return report
