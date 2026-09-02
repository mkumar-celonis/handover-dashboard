from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from scanner.files import match_glob, read_text, rel


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _files_exist(root: Path, patterns: list[str], files: list[Path]) -> list[str]:
    hits: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for path in match_glob(root, pattern, files):
            key = rel(root, path)
            if key not in seen:
                seen.add(key)
                hits.append(key)
            if len(hits) >= 12:
                return hits
    return hits


def _content_hits(
    root: Path,
    glob_pat: str,
    patterns: list[str],
    files: list[Path],
    max_files: int = 8,
) -> list[dict[str, str]]:
    import re

    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    hits: list[dict[str, str]] = []
    candidates = match_glob(root, glob_pat, files, limit=80)
    for path in candidates:
        text = read_text(path)
        if not text:
            continue
        for regex in compiled:
            match = regex.search(text)
            if match:
                line_no = text[: match.start()].count("\n") + 1
                hits.append(
                    {
                        "file": rel(root, path),
                        "line": str(line_no),
                        "snippet": match.group(0)[:80],
                    }
                )
                break
        if len(hits) >= max_files:
            break
    return hits


@dataclass
class CheckResult:
    id: str
    area: str
    weight: float
    status: str  # found | partial | missing
    score: float  # 0..1
    evidence: str
    files: list[str] = field(default_factory=list)
    hits: list[dict[str, str]] = field(default_factory=list)
    note: str | None = None
    escalate_if_missing: str | None = None
    invert: bool = False


def _eval_files_clause(root: Path, spec: dict[str, Any], files: list[Path]) -> tuple[float, list[str], list[dict[str, str]]]:
    if "any_files" in spec:
        found = _files_exist(root, spec["any_files"], files)
        min_bytes = spec.get("min_bytes")
        if found and min_bytes:
            substantial = []
            for relative in found:
                size = (root / relative).stat().st_size
                if size >= min_bytes:
                    substantial.append(relative)
            if not substantial:
                return 0.4, found, []
            return 1.0, substantial, []
        return (1.0 if found else 0.0), found, []

    if "content_any" in spec:
        clause = spec["content_any"]
        hits = _content_hits(root, clause.get("glob", "**/*"), clause["patterns"], files)
        files_hit = [h["file"] for h in hits]
        return (1.0 if hits else 0.0), files_hit, hits

    return 0.0, [], []


def _eval_any_of(root: Path, clauses: list[dict[str, Any]], files: list[Path]) -> tuple[float, list[str], list[dict[str, str]]]:
    best_score = 0.0
    all_files: list[str] = []
    all_hits: list[dict[str, str]] = []
    for clause in clauses:
        wrapped: dict[str, Any] = {}
        if "files" in clause:
            wrapped["any_files"] = clause["files"]
        if "content_any" in clause:
            wrapped["content_any"] = clause["content_any"]
        score, found, hits = _eval_files_clause(root, wrapped, files)
        all_files.extend(found)
        all_hits.extend(hits)
        best_score = max(best_score, score)
    seen: set[str] = set()
    unique = []
    for item in all_files:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return best_score, unique[:12], all_hits[:8]


def run_check(root: Path, spec: dict[str, Any], files: list[Path]) -> CheckResult:
    invert = bool(spec.get("invert"))
    if "any_of" in spec:
        raw_score, found, hits = _eval_any_of(root, spec["any_of"], files)
    else:
        raw_score, found, hits = _eval_files_clause(root, spec, files)

    if invert:
        if raw_score >= 1.0:
            status, score = "missing", 0.0
        elif raw_score > 0:
            status, score = "partial", 0.5
        else:
            status, score = "found", 1.0
    else:
        if raw_score >= 1.0:
            status, score = "found", 1.0
        elif raw_score > 0:
            status, score = "partial", raw_score
        else:
            status, score = "missing", 0.0

    return CheckResult(
        id=spec["id"],
        area=spec["area"],
        weight=float(spec.get("weight", 1)),
        status=status,
        score=score,
        evidence=spec.get("evidence", spec["id"]),
        files=found,
        hits=hits,
        note=spec.get("note"),
        escalate_if_missing=spec.get("escalate_if_missing"),
        invert=invert,
    )


def rag_from_score(score_0_5: float) -> str:
    if score_0_5 >= 4.0:
        return "green"
    if score_0_5 >= 3.0:
        return "amber"
    return "red"


def recompute_overall(report: dict[str, Any], extra_p0_reds: list[str] | None = None, extra_p0_ambers: list[str] | None = None) -> dict[str, Any]:
    """Recompute headline score/RAG from area rows, keeping non-area P0 ids."""
    area_ids = {str(a.get("id") or "") for a in report.get("areas") or []}
    keep_reds = [
        x
        for x in (extra_p0_reds if extra_p0_reds is not None else report.get("p0_reds") or [])
        if x not in area_ids
    ]
    keep_ambers = [
        x
        for x in (extra_p0_ambers if extra_p0_ambers is not None else report.get("p0_ambers") or [])
        if x not in area_ids
    ]
    weighted = 0.0
    total = 0.0
    p0_reds = list(keep_reds)
    p0_ambers = list(keep_ambers)
    for area in report.get("areas") or []:
        w = float(area.get("weight") or 0)
        s = float(area.get("score") or 0)
        weighted += s * w
        total += w
        if area.get("p0"):
            if area.get("rag") == "red" and area.get("id") not in p0_reds:
                p0_reds.append(area["id"])
            elif area.get("rag") == "amber" and area.get("id") not in p0_ambers:
                p0_ambers.append(area["id"])
    overall_0_5 = weighted / total if total else 0
    report["overall_score"] = round(overall_0_5, 2)
    report["overall_pct"] = round((overall_0_5 / 5) * 100)
    report["p0_reds"] = list(dict.fromkeys(p0_reds))
    report["p0_ambers"] = list(dict.fromkeys(p0_ambers))
    verdict = decision(report["overall_pct"], report["p0_reds"], report["p0_ambers"])
    report["verdict"] = verdict
    report["overall_rag"] = {"block": "red", "conditional": "amber", "go": "green"}[verdict["verdict"]]
    return report


def decision(overall_pct: float, p0_reds: list[str], p0_ambers: list[str]) -> dict[str, str]:
    if overall_pct < 60 or p0_reds:
        return {
            "verdict": "block",
            "label": "Block / escalate",
            "detail": "Do not complete primary ownership transfer until P0 Reds are closed or leadership accepts residual risk.",
        }
    if overall_pct < 80 or p0_ambers:
        return {
            "verdict": "conditional",
            "label": "Conditional handover",
            "detail": "Shadow period plus a dated close-the-gap plan. Old team stays on backup pager.",
        }
    return {
        "verdict": "go",
        "label": "Handover can complete",
        "detail": "No P0 Red. Residual work can sit on the normal backlog.",
    }


def score_report(config: dict[str, Any], results: list[CheckResult]) -> dict[str, Any]:
    areas_cfg = config["areas"]
    by_area: dict[str, list[CheckResult]] = {key: [] for key in areas_cfg}
    for result in results:
        by_area.setdefault(result.area, []).append(result)

    area_rows = []
    weighted_sum = 0.0
    weight_total = 0.0
    p0_reds: list[str] = []
    p0_ambers: list[str] = []

    for key, meta in areas_cfg.items():
        checks = by_area.get(key, [])
        weight_sum = sum(c.weight for c in checks) or 1.0
        ratio = sum(c.score * c.weight for c in checks) / weight_sum
        score_0_5 = round(ratio * 5, 1)
        rag = rag_from_score(score_0_5)
        area_weight = float(meta["weight"])
        weighted_sum += score_0_5 * area_weight
        weight_total += area_weight
        if meta.get("p0"):
            if rag == "red":
                p0_reds.append(key)
            elif rag == "amber":
                p0_ambers.append(key)
        missing = [c for c in checks if c.status == "missing"]
        found = [c for c in checks if c.status == "found"]
        why_bits = []
        if found:
            why_bits.append("Found: " + ", ".join(c.evidence for c in found[:3]))
        if missing:
            why_bits.append("Missing: " + ", ".join(c.evidence for c in missing[:3]))
        area_rows.append(
            {
                "id": key,
                "label": meta["label"],
                "weight": area_weight,
                "p0": bool(meta.get("p0")),
                "score": score_0_5,
                "score_pct": round(ratio * 100),
                "rag": rag,
                "score_5_looks_like": meta.get("score_5", ""),
                "why": " ".join(why_bits) if why_bits else "No checks ran.",
                "checks": [check_to_dict(c) for c in checks],
            }
        )

    overall_0_5 = weighted_sum / weight_total if weight_total else 0
    overall_pct = round((overall_0_5 / 5) * 100)
    verdict = decision(overall_pct, p0_reds, p0_ambers)
    overall_rag = {"block": "red", "conditional": "amber", "go": "green"}[verdict["verdict"]]

    gaps = []
    for result in results:
        if result.status == "missing" and result.escalate_if_missing:
            gaps.append(
                {
                    "id": result.id,
                    "area": result.area,
                    "title": result.evidence,
                    "ask": result.escalate_if_missing,
                    "p0": bool(areas_cfg.get(result.area, {}).get("p0")),
                }
            )

    plan = build_plan(area_rows, gaps)
    return {
        "overall_score": round(overall_0_5, 2),
        "overall_pct": overall_pct,
        "overall_rag": overall_rag,
        "verdict": verdict,
        "p0_reds": p0_reds,
        "p0_ambers": p0_ambers,
        "areas": area_rows,
        "gaps": gaps,
        "plan": plan,
        "limits": [
            "This is a git-artifact scan. It cannot score named on-call people, game-days, or live Datadog/Argo health.",
            "A well-run service whose ops live in another repo will be under-scored.",
            "A wiki-heavy repo can be over-scored if nobody has actually recovered an incident from the runbooks.",
        ],
    }


def check_to_dict(result: CheckResult) -> dict[str, Any]:
    return {
        "id": result.id,
        "area": result.area,
        "weight": result.weight,
        "status": result.status,
        "score": result.score,
        "evidence": result.evidence,
        "files": result.files,
        "hits": result.hits,
        "note": result.note,
        "escalate_if_missing": result.escalate_if_missing,
        "invert": result.invert,
    }


def build_plan(areas: list[dict[str, Any]], gaps: list[dict[str, Any]]) -> list[dict[str, str]]:
    weeks: list[dict[str, str]] = []
    p0_gaps = [g for g in gaps if g.get("p0")]
    other = [g for g in gaps if not g.get("p0")]
    red_areas = [a for a in areas if a["rag"] == "red"]
    amber_areas = [a for a in areas if a["rag"] == "amber"]

    weeks.append(
        {
            "week": "1",
            "outcome": "Named primary + backup; grant Argo/Datadog/on-call access; fill people column of this scorecard.",
            "maps_to": "Ownership",
        }
    )
    if any(g["area"] == "security" for g in p0_gaps) or any(a["id"] == "security" for a in red_areas):
        weeks.append(
            {
                "week": "1",
                "outcome": "Authz / secrets / security pack: decision tree and kill switches the new team can operate.",
                "maps_to": "Security",
            }
        )
    if any(a["id"] == "architecture" for a in red_areas + amber_areas):
        weeks.append(
            {
                "week": "2",
                "outcome": "One-page architecture + dependency paging map; add ADRs for non-obvious design.",
                "maps_to": "Architecture",
            }
        )
    if any(a["id"] == "operate" for a in red_areas + amber_areas) or any(g["area"] == "operate" for g in p0_gaps):
        weeks.append(
            {
                "week": "2–3",
                "outcome": "Close runbook TODOs; game-days: service down, data path, rollback — new team leads.",
                "maps_to": "Operate",
            }
        )
    if any(a["id"] == "knowledge" for a in red_areas + amber_areas):
        weeks.append(
            {
                "week": "3",
                "outcome": "Golden-path local setup that a stranger completes in half a day.",
                "maps_to": "Knowledge",
            }
        )
    if any(a["id"] in ("data", "release") for a in red_areas + amber_areas):
        weeks.append(
            {
                "week": "3",
                "outcome": "Document restore/rollback as practiced steps; own leftover migrations as tickets.",
                "maps_to": "Data / Release",
            }
        )
    if other:
        weeks.append(
            {
                "week": "4",
                "outcome": "Non-P0 gaps (APIs, SLOs, feature flags, README) as normal backlog — do not block cutover.",
                "maps_to": "Product / Knowledge",
            }
        )
    weeks.append(
        {
            "week": "4",
            "outcome": "Cutover decision: go / conditional / delay. Old team backup-only if go.",
            "maps_to": "Go / no-go",
        }
    )
    return weeks
