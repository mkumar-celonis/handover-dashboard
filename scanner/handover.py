from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

# Default risk weights (not even). Reverse shadowing carries the most.
# Re-tune per service via checks.yaml, catalog annotations, or repo handover.yaml.
DEFAULT_WEIGHTS = {"docs": 25, "observe": 25, "shadow": 30, "debt": 20}
PASS_RATIO = 0.8
GREEN_RATIO = 0.85
AMBER_RATIO = 0.70

PILLARS = [
    {
        "id": "docs",
        "label": "Documentation & SOPs",
        "max": 25,
        "pass": 20,
        "areas": {
            "architecture": 0.28,
            "knowledge": 0.28,
            "operate": 0.22,
            "release": 0.12,
            "data": 0.10,
        },
        "criteria": "Architecture & data flow, runbooks, Drive KT recordings/docs (quality, not file count), README, env docs.",
    },
    {
        "id": "observe",
        "label": "Observability & Access",
        "max": 25,
        "pass": 20,
        "areas": {"operate": 0.50, "ownership": 0.30, "security": 0.20},
        "criteria": "Dashboards, alerts, logs/traces, on-call routes, access transferred.",
    },
    {
        "id": "shadow",
        "label": "Reverse Shadowing",
        "max": 30,
        "pass": 24,
        "areas": {},
        "criteria": "Incoming observed live work, then resolved ≥2 incidents without intervention.",
    },
    {
        "id": "debt",
        "label": "Backlog & Tech Debt",
        "max": 20,
        "pass": 15,
        "areas": {"security": 0.70, "data": 0.15, "product": 0.15},
        "criteria": "Critical vulns closed; known bugs and vendors documented with owners.",
    },
]


def _int_weight(value: Any, fallback: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, min(80, n))


def _parse_weight_blob(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in re.split(r"[,;\s]+", (text or "").strip()):
        if not part or ("=" not in part and ":" not in part):
            continue
        key, raw = part.split("=" if "=" in part else ":", 1)
        key = key.strip()
        if key in DEFAULT_WEIGHTS:
            out[key] = _int_weight(raw, DEFAULT_WEIGHTS[key])
    return out


def _merge_weights(base: dict[str, int], incoming: Any) -> dict[str, int]:
    if not incoming:
        return base
    merged = dict(base)
    if isinstance(incoming, dict):
        for key in DEFAULT_WEIGHTS:
            if key in incoming:
                merged[key] = _int_weight(incoming[key], base[key])
    elif isinstance(incoming, str):
        merged.update(_parse_weight_blob(incoming))
    return merged


def shadow_buckets(maximum: int) -> tuple[int, int, int]:
    maximum = max(0, int(maximum or 0))
    observe = maximum // 3
    first = maximum // 3
    second = maximum - observe - first
    return observe, first, second


def resolve_pillars(
    root: Path | None = None,
    catalog: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    ui_weights: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    weights = dict(DEFAULT_WEIGHTS)
    source = "default"
    if config and config.get("pillars") is not None:
        weights = _merge_weights(weights, config.get("pillars"))
        source = "dashboard"
    annotations = (catalog or {}).get("annotations") or {}
    blob = annotations.get("handover.celonis.dev/weights")
    if blob:
        weights = _merge_weights(weights, blob)
        source = "catalog"
    for key, val in annotations.items():
        prefix = "handover.celonis.dev/weights."
        if key.startswith(prefix):
            pid = key[len(prefix) :]
            if pid in DEFAULT_WEIGHTS:
                weights[pid] = _int_weight(val, weights[pid])
                source = "catalog"
    if root:
        for name in ("handover.yaml", ".handover.yaml"):
            path = root / name
            if not path.is_file():
                continue
            try:
                import yaml

                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                break
            if isinstance(data, dict):
                weights = _merge_weights(weights, data.get("pillars") or data.get("weights"))
                source = "repo"
            break
    if ui_weights:
        weights = _merge_weights(weights, ui_weights)
        source = "ui"
    specs = []
    for meta in PILLARS:
        spec = deepcopy(meta)
        maximum = weights.get(spec["id"], spec["max"])
        spec["max"] = maximum
        if maximum == meta["max"]:
            spec["pass"] = meta["pass"]
        else:
            spec["pass"] = max(0, int(round(maximum * PASS_RATIO)))
        specs.append(spec)
    meta = {
        "source": source,
        "weights": {s["id"]: s["max"] for s in specs},
        "total": sum(s["max"] for s in specs),
        "note": "Weighted by risk, not evenly. Reverse shadowing carries the most.",
    }
    return specs, meta


def default_signoff() -> dict[str, Any]:
    return {
        "kt_recorded": False,
        "dashboards_verified": False,
        "access_transferred": False,
        "primary_shadowing": False,
        "reverse_incidents": 0,
    }


def _area_row(report: dict[str, Any], area_id: str) -> dict[str, Any]:
    for area in report.get("areas") or []:
        if area.get("id") == area_id:
            return area
    return {}


def _area_ratio(report: dict[str, Any], area_id: str) -> float:
    area = _area_row(report, area_id)
    return max(0.0, min(1.0, float(area.get("score") or 0) / 5.0))


def _breakdown(report: dict[str, Any], spec: dict[str, Any], signoff: dict[str, Any]) -> list[dict[str, Any]]:
    if spec["id"] == "shadow":
        incidents = int(signoff.get("reverse_incidents") or 0)
        observe, first, second = shadow_buckets(spec["max"])
        return [
            {
                "label": "Incoming shadowed live incidents & deploys",
                "points": observe if signoff.get("primary_shadowing") else 0,
                "max": observe,
                "done": bool(signoff.get("primary_shadowing")),
                "why": "Tick People sign-off. Git cannot prove shadowing.",
            },
            {
                "label": "Incoming led 1 incident (outgoing backup)",
                "points": first if incidents >= 1 else 0,
                "max": first,
                "done": incidents >= 1,
                "why": "First reverse-shadow incident without outgoing taking over.",
            },
            {
                "label": "≥2 incidents without intervention",
                "points": second if incidents >= 2 else 0,
                "max": second,
                "done": incidents >= 2,
                "why": f"Passing threshold is {spec['pass']}/{spec['max']} — need both reverse-shadow incidents.",
            },
        ]
    rows = []
    for area_id, weight in spec["areas"].items():
        area = _area_row(report, area_id)
        ratio = _area_ratio(report, area_id)
        rows.append(
            {
                "area_id": area_id,
                "label": area.get("label") or area_id,
                "score": area.get("score"),
                "rag": area.get("rag"),
                "weight": weight,
                "points": round(ratio * weight * spec["max"], 1),
                "max": round(weight * spec["max"], 1),
                "why": "",
            }
        )
    if spec["id"] == "docs":
        rb = report.get("runbooks") or {}
        if rb.get("status"):
            rows.insert(
                0,
                {
                    "label": "Runbook quality",
                    "score": rb.get("score"),
                    "rag": rb.get("rag"),
                    "why": rb.get("summary") or "",
                },
            )
        conf = report.get("confluence") or {}
        if conf.get("status") == "connected":
            rows.insert(
                0,
                {
                    "label": "Confluence / Celospace",
                    "score": conf.get("docs_score") or conf.get("score"),
                    "rag": conf.get("rag"),
                    "why": conf.get("summary") or "",
                },
            )
        drive = report.get("drive") or {}
        if drive.get("status") == "connected":
            rows.insert(
                0,
                {
                    "label": "Google Drive KT pack",
                    "score": drive.get("score"),
                    "rag": drive.get("rag"),
                    "why": drive.get("summary") or "",
                },
            )
        if signoff.get("kt_recorded"):
            rows.append({"label": "KT walkthrough recorded", "points": 3, "max": 3, "done": True, "why": "People sign-off bonus."})
        elif drive.get("kt_ready"):
            rows.append(
                {
                    "label": "Drive KT recordings exist",
                    "points": 2,
                    "max": 3,
                    "done": True,
                    "why": f"{drive.get('kt_recordings', 0)} handover recordings in Drive. Tick sign-off when incoming has watched them.",
                }
            )
    if spec["id"] == "observe":
        if signoff.get("dashboards_verified"):
            rows.append({"label": "Dashboards + logs verified", "points": 2, "max": 2, "done": True, "why": "People sign-off bonus."})
        if signoff.get("access_transferred"):
            rows.append({"label": "Pager / repo / secrets access transferred", "points": 3, "max": 3, "done": True, "why": "People sign-off bonus."})
    return rows


def _shadow_points(signoff: dict[str, Any], maximum: int) -> int:
    observe, first, second = shadow_buckets(maximum)
    points = 0
    if signoff.get("primary_shadowing"):
        points += observe
    incidents = int(signoff.get("reverse_incidents") or 0)
    if incidents >= 1:
        points += first
    if incidents >= 2:
        points += second
    return min(maximum, points)


def apply_handover(
    report: dict[str, Any],
    signoff: dict[str, Any] | None = None,
    *,
    root: Path | None = None,
    catalog: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    ui_weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Risk-weighted exit scorecard. Default 25 docs / 25 observe / 30 shadow / 20 debt."""
    signoff = {**default_signoff(), **(signoff or {})}
    try:
        signoff["reverse_incidents"] = max(0, min(2, int(signoff.get("reverse_incidents") or 0)))
    except (TypeError, ValueError):
        signoff["reverse_incidents"] = 0

    specs, weight_meta = resolve_pillars(
        root=root, catalog=catalog, config=config, ui_weights=ui_weights
    )
    rows: list[dict[str, Any]] = []
    for spec in specs:
        if spec["id"] == "shadow":
            earned = float(_shadow_points(signoff, spec["max"]))
            git_points = 0.0
        else:
            ratio = 0.0
            for area_id, weight in spec["areas"].items():
                ratio += _area_ratio(report, area_id) * weight
            git_points = round(ratio * spec["max"], 1)
            earned = git_points
            if spec["id"] == "docs":
                drive = report.get("drive") or {}
                if signoff.get("kt_recorded"):
                    earned = min(spec["max"], earned + 3)
                elif drive.get("kt_ready"):
                    earned = min(spec["max"], earned + 2)
            if spec["id"] == "observe":
                if signoff.get("dashboards_verified"):
                    earned = min(spec["max"], earned + 2)
                if signoff.get("access_transferred"):
                    earned = min(spec["max"], earned + 3)
            earned = round(earned, 1)
        passing = spec["pass"]
        maximum = spec["max"]
        below = earned < passing
        rows.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "criteria": spec["criteria"],
                "max": maximum,
                "pass": passing,
                "git_points": git_points,
                "git_ratio": round(git_points / maximum, 4) if maximum else 0,
                "earned": earned,
                "below_pass": below,
                "rag": "red" if below and earned < passing * 0.75 else ("amber" if below else "green"),
                "breakdown": _breakdown(report, spec, signoff),
            }
        )

    points = int(round(sum(p["earned"] for p in rows)))
    total = int(weight_meta.get("total") or 100) or 100
    green_cut = int(round(total * GREEN_RATIO))
    amber_cut = int(round(total * AMBER_RATIO))
    failing = [p["label"] for p in rows if p["below_pass"]]
    shadow = next(p for p in rows if p["id"] == "shadow")
    observe_need, _, _ = shadow_buckets(int(shadow["max"]))
    shadow_ok = observe_need == 0 or shadow["earned"] >= observe_need
    all_pass = not failing

    if points >= green_cut and all_pass:
        verdict = {
            "verdict": "go",
            "label": "Complete handover",
            "detail": "Incoming team takes 100% operational ownership. Outgoing team disengages.",
            "action": "Sign the cutover. Old team leaves the pager.",
        }
    elif points >= amber_cut and shadow_ok:
        verdict = {
            "verdict": "conditional",
            "label": "Soft handover",
            "detail": "Incoming team takes primary on-call. Outgoing stays on secondary escalation for 2 more weeks to close gaps"
            + (f" ({', '.join(failing)})." if failing else "."),
            "action": "2-week bridge. Close failing pillars before outgoing leaves.",
        }
    else:
        why = ", ".join(failing) if failing else f"score below {amber_cut}"
        verdict = {
            "verdict": "block",
            "label": "Block handover",
            "detail": f"Transition halted. Escalate to leadership: {why}.",
            "action": "Do not accept support ownership. Send the failing pillars to leadership.",
        }

    report["signoff"] = signoff
    report["pillars"] = rows
    report["handover"] = {
        "points": points,
        "max": total,
        "failing": failing,
        "thresholds": {"green": green_cut, "amber": amber_cut},
        "weights": weight_meta.get("weights") or {},
        "weights_source": weight_meta.get("source") or "default",
        "weighted_by": "risk",
        "note": weight_meta.get("note"),
    }
    report["verdict"] = verdict
    report["overall_pct"] = points
    report["overall_score"] = round((points / total) * 5, 2) if total else 0
    report["overall_rag"] = {"block": "red", "conditional": "amber", "go": "green"}[verdict["verdict"]]
    report["gaps"] = [g for g in (report.get("gaps") or []) if not str(g.get("id") or "").startswith("handover.")]
    for p in rows:
        if not p["below_pass"]:
            continue
        gid = f"handover.{p['id']}"
        if any(g.get("id") == gid for g in report.get("gaps") or []):
            continue
        report.setdefault("gaps", []).append(
            {
                "id": gid,
                "area": p["id"],
                "title": f"{p['label']} below {p['pass']}/{p['max']}",
                "ask": p["criteria"],
                "p0": p["id"] in {"docs", "observe", "shadow"} or p["earned"] < p["pass"] * 0.5,
            }
        )
    return report
