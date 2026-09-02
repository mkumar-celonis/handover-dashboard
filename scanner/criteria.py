"""Finalised handover criteria and scoring mechanisms.

This is the list the dashboard evaluates. Numbers match the scanners:
handover.py, score.py, checks.yaml, github.py, datadog.py, jira.py, roadie.py,
drive.py, confluence.py, runbooks.py, platform.py, celonis.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scanner.score import load_config

CONFIG_PATH = Path(__file__).parent / "checks.yaml"

AREA_RAG = "Green ≥4.0 / amber 3.0–3.9 / red <3.0 (on a 0–5 scale)."
BLEND_55 = "55% git artifact + 45% live source."
WINDOW = "Live counts use the selected lookback (default 90 days). Monitors and SLOs are current-state."


def _check_how(spec: dict[str, Any]) -> str:
    bits: list[str] = []
    if spec.get("invert"):
        bits.append("Fails if the signal is found (hygiene / stub check).")
    if spec.get("min_bytes"):
        bits.append(f"File must be at least {spec['min_bytes']} bytes.")
    if "any_files" in spec:
        bits.append("Looks for any of: " + ", ".join(spec["any_files"][:8]))
    if "content_any" in spec:
        clause = spec["content_any"]
        pats = ", ".join(str(p) for p in (clause.get("patterns") or [])[:6])
        bits.append(f"Text match in {clause.get('glob', '**/*')}: {pats}")
    if "any_of" in spec:
        alts = []
        for clause in spec["any_of"]:
            if "files" in clause:
                alts.append("files " + ", ".join(clause["files"][:4]))
            if "content_any" in clause:
                pats = ", ".join(str(p) for p in (clause["content_any"].get("patterns") or [])[:4])
                alts.append(f"text {pats}")
        bits.append("Any of: " + " · ".join(alts))
    if spec.get("note"):
        bits.append(str(spec["note"]))
    return " ".join(bits) or "File or content presence."


def _git_areas(config: dict[str, Any]) -> list[dict[str, Any]]:
    checks_by_area: dict[str, list[dict[str, Any]]] = {}
    for spec in config.get("checks") or []:
        checks_by_area.setdefault(spec.get("area"), []).append(spec)
    rows: list[dict[str, Any]] = []
    for area_id, meta in (config.get("areas") or {}).items():
        weight = int(round(float(meta.get("weight") or 0) * 100))
        checks = checks_by_area.get(area_id) or []
        rows.append(
            {
                "criterion": meta.get("label") or area_id,
                "mechanism": (
                    f"Git checks in this area are weighted, then scaled to 0–5. "
                    f"Each check is found (1.0), partial (0.5), or missing (0.0). "
                    f"Area weight {weight}% of the git scorecard. "
                    f"A 5/5 looks like: {meta.get('score_5') or 'complete coverage'}."
                ),
                "green": "Area ≥ 4.0",
                "amber": "3.0–3.9",
                "red": "< 3.0",
                "p0": bool(meta.get("p0")),
                "checks": [
                    {
                        "id": c.get("id"),
                        "evidence": c.get("evidence") or c.get("id"),
                        "weight": c.get("weight"),
                        "how": _check_how(c),
                        "escalate": c.get("escalate_if_missing"),
                    }
                    for c in checks
                ],
            }
        )
    return rows


def criteria_catalog() -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    return {
        "title": "Criteria & scoring",
        "summary": (
            "Exit score is out of 100, weighted by risk (not evenly). "
            "Git artifacts fill most pillars; reverse shadowing is people sign-off. "
            "Live sources blend into git areas, then pillars are computed last. "
            + AREA_RAG
            + " "
            + WINDOW
        ),
        "exec": {
            "question": "Can the incoming team take operational ownership without the outgoing team on the pager?",
            "decision": [
                {
                    "rag": "green",
                    "label": "Complete handover",
                    "when": "Score ≥ 85 and every pillar passes",
                    "means": "Incoming owns 100%. Outgoing leaves the pager.",
                },
                {
                    "rag": "amber",
                    "label": "Soft handover",
                    "when": "Score 70–84, and incoming has at least shadowed live work",
                    "means": "Incoming is primary. Outgoing stays secondary for 2 weeks to close gaps.",
                },
                {
                    "rag": "red",
                    "label": "Block",
                    "when": "Score < 70, or shadowing never started, or a critical gap is still open",
                    "means": "Do not transfer support. Escalate the failing pillars.",
                },
            ],
            "pillars": [
                {
                    "label": "Reverse shadowing",
                    "weight": 30,
                    "pass": 24,
                    "ask": "Incoming has watched live work, then resolved two incidents without help.",
                },
                {
                    "label": "Documentation & SOPs",
                    "weight": 25,
                    "pass": 20,
                    "ask": "Architecture, runbooks, and KT material are good enough that a stranger can operate.",
                },
                {
                    "label": "Observability & access",
                    "weight": 25,
                    "pass": 20,
                    "ask": "Dashboards, alerts, on-call routes, and access have moved to the new team.",
                },
                {
                    "label": "Backlog & tech debt",
                    "weight": 20,
                    "pass": 15,
                    "ask": "Critical vulnerabilities are closed; known risk has a named owner.",
                },
            ],
            "rag": (
                "Green means ready, amber means conditional, red means stop. "
                "On any 0–5 check, green is ≥4.0 (~80%), amber 3.0–3.9, red below 3.0. "
                "A red P0 — burning pager, unprotected default branch, no monitors, "
                "or Kargo missing on a cloud service — can block the cutover even if the headline looks high."
            ),
            "sources": [
                {"name": "People sign-off", "role": "Shadowing and access. Git cannot prove this."},
                {"name": "Git", "role": "Docs, runbooks, CI, owners, security hygiene."},
                {"name": "Datadog", "role": "Live pager: monitors, SLOs, Sev-1s."},
                {"name": "GitHub", "role": "Branch protection, reviews, E2E."},
                {"name": "Confluence & Drive", "role": "SOPs, handover pack, KT recordings."},
                {"name": "Jira, Roadie, Kargo, Code Purple", "role": "Backlog, catalog bar, deploy path, quality lane."},
            ],
            "group": "For a squad, the headline is the average. The verdict is the weakest service — one blocked service blocks the group.",
        },
        "bands": [
            {
                "label": "Git / live area (0–5)",
                "green": "≥ 4.0 (~80%)",
                "amber": "3.0–3.9",
                "red": "< 3.0",
            },
            {
                "label": "Exit pillar",
                "green": "At or above pass (80% of that pillar’s max)",
                "amber": "Below pass, but ≥ 75% of pass",
                "red": "Below 75% of pass",
            },
            {
                "label": "Handover decision",
                "green": "≥ 85 and every pillar passes → complete handover",
                "amber": "70–84 and incoming has at least shadowed → soft handover (outgoing stays 2 weeks)",
                "red": "< 70, or no shadowing, or a failing pillar below the amber cut → block",
            },
        ],
        "sections": [
            {
                "id": "decision",
                "title": "Exit decision",
                "intro": (
                    "Headline uses handover.py after all live blends. Group verdict is the weakest "
                    "selected service; group score is the average."
                ),
                "rows": [
                    {
                        "criterion": "Complete handover",
                        "mechanism": "Points ≥ 85 (or 85% of retuned total) and every pillar is at or above its pass line.",
                        "green": "Incoming takes 100% ownership. Outgoing leaves the pager.",
                        "amber": "—",
                        "red": "—",
                    },
                    {
                        "criterion": "Soft handover",
                        "mechanism": "Points ≥ 70 and incoming has completed the observe-shadowing bucket (1/3 of Reverse Shadowing).",
                        "green": "—",
                        "amber": "Incoming is primary. Outgoing stays secondary for 2 weeks.",
                        "red": "—",
                    },
                    {
                        "criterion": "Block handover",
                        "mechanism": "Points < 70, or shadowing not started, or any pillar still below pass when the total is also below 85.",
                        "green": "—",
                        "amber": "—",
                        "red": "Do not accept support ownership. Escalate failing pillars.",
                    },
                    {
                        "criterion": "P0 gaps",
                        "mechanism": (
                            "Missing escalate_if_missing checks on P0 git areas, unprotected default branch, "
                            "Datadog Alert monitors / SLO budget <10% / no monitors on a service, Kargo missing "
                            "on a cloud service, Code Purple incomplete on tier-1, or a failing docs/observe/shadow pillar."
                        ),
                        "green": "No P0 gaps",
                        "amber": "Shown in Blockers; may still be a soft handover",
                        "red": "Listed as Blockers and can force block",
                        "p0": True,
                    },
                ],
            },
            {
                "id": "pillars",
                "title": "Exit pillars (risk weights)",
                "intro": (
                    "Defaults: Reverse shadowing 30, Documentation 25, Observability 25, Backlog 20. "
                    "Pass is 80% of each pillar. Retune with sliders, repo handover.yaml, or catalog "
                    "handover.celonis.dev/weights. Total stays 100."
                ),
                "rows": [
                    {
                        "criterion": "Reverse Shadowing (30, pass 24)",
                        "mechanism": (
                            "People sign-off only — git cannot prove this. Max is split into three buckets: "
                            "shadow live work (10), incoming led 1 incident (10), ≥2 incidents without intervention (10). "
                            "Need both reverse-shadow incidents to pass."
                        ),
                        "green": "≥ 24 (shadowed + 2 incidents)",
                        "amber": "18–23",
                        "red": "< 18, or 0 if shadowing never started (blocks the cutover)",
                    },
                    {
                        "criterion": "Documentation & SOPs (25, pass 20)",
                        "mechanism": (
                            "Weighted git areas: architecture 28%, knowledge 28%, operate 22%, release 12%, data 10%. "
                            "+2 if Drive KT recordings exist (≥3 handover videos). +3 if ‘KT recorded’ is ticked. "
                            "Runbook quality is shown in the breakdown and already blended into Operate."
                        ),
                        "green": "≥ 20",
                        "amber": "15–19",
                        "red": "< 15",
                    },
                    {
                        "criterion": "Observability & Access (25, pass 20)",
                        "mechanism": (
                            "Weighted git/live areas: operate 50%, ownership 30%, security 20%. "
                            "+2 if dashboards verified. +3 if pager / repo / secrets access transferred."
                        ),
                        "green": "≥ 20",
                        "amber": "15–19",
                        "red": "< 15",
                    },
                    {
                        "criterion": "Backlog & Tech Debt (20, pass 15)",
                        "mechanism": "Weighted git/live areas: security 70%, data 15%, product 15%. No people-sign-off bonus.",
                        "green": "≥ 15",
                        "amber": "12–14",
                        "red": "< 12",
                    },
                ],
            },
            {
                "id": "git",
                "title": "Git areas & file checks",
                "intro": (
                    "Filesystem scan of the local clone. found / partial / missing, then weighted inside the area and scaled to 0–5. "
                    "P0 areas (ownership, architecture, operate, security, release, data, testing) can block when red."
                ),
                "rows": _git_areas(config),
            },
            {
                "id": "live",
                "title": "Live sources",
                "intro": (
                    "Connected APIs overlay git areas, then pillars are computed. "
                    "Jira RAG is shown on Live sources but is not blended into the exit score today. "
                    + WINDOW
                ),
                "rows": [
                    {
                        "criterion": "Datadog — operate",
                        "mechanism": (
                            "Monitors tagged service:<name>, SLOs, and incidents in the window. "
                            "Start 3.0. +0.8 if ≥5 monitors, +0.4 if 1–4, −1.2 if a service has none. "
                            "−0.35 per Alert (cap 1.5). −0.1 per Warn (cap 0.6). −0.3 if >3 muted. "
                            "+0.5 if an SLO is OK, +0.2 if SLOs exist but are not OK. −1.0 if error budget <10%. "
                            "−0.4 per Sev-1 / P0 (cap 1.2). +0.15 if there were incidents but no Sev-1 "
                            "(process has been exercised). Blend: " + BLEND_55 + " Then runbook quality is blended again (55% that mix + 45% quality)."
                        ),
                        "green": "Live operate ≥ 4.0 after blends",
                        "amber": "3.0–3.9, or Datadog not connected (git-only)",
                        "red": "< 3.0, or Alert monitors / SLO budget <10% / no monitors on a service (P0)",
                        "p0": True,
                    },
                    {
                        "criterion": "GitHub — change / release",
                        "mechanism": (
                            "Start 3.0. Default-branch protection +0.8 / unprotected −1.5 (P0). "
                            "Review coverage: +0.5 if ≥85% of sampled merged PRs, −0.8 if <70%. "
                            "Median merge: +0.3 if ≤16h, −0.5 if >48h. "
                            "Commits/week: +0.4 if ≥2, −0.6 otherwise. "
                            "Deploy frequency from releases/week: high ≥1/wk +0.3, medium ≥0.25 +0.1, else −0.3. "
                            "Blend into Release: " + BLEND_55
                        ),
                        "green": "Live GitHub ≥ 4.0",
                        "amber": "3.0–3.9, or token missing",
                        "red": "< 3.0, or unprotected default branch (P0)",
                        "p0": True,
                    },
                    {
                        "criterion": "GitHub — E2E / test automation",
                        "mechanism": (
                            "Workflows whose name/path match E2E, Playwright, Cypress, Selenium, or ‘test automation’ "
                            "(e.g. RepoDepot Run E2E Tests). No workflow → 1.0. Workflow but no runs in window → 2.2. "
                            "Success rate: ≥90% → 4.6, ≥75% → 4.0, ≥50% → 3.0, else 1.8. "
                            "Last run success +0.3, last failure −0.8. Blend into Testing: 40% git + 60% live E2E."
                        ),
                        "green": "Success ≥75% and last run not a failure (score ≥ 4.0)",
                        "amber": "3.0–3.9 (about 50–75% success, or workflow with no recent runs)",
                        "red": "< 3.0 (no suite, or mostly failing)",
                        "p0": True,
                    },
                    {
                        "criterion": "Jira — CBE / security / features / bugs",
                        "mechanism": (
                            "Counts in the window: CBE (Service internal, not Vulnerability), CBE vulnerabilities, "
                            "TMT Stories/Epics, TMT Bugs. Start 4.0. −1.2 if security ≥5 or CBE ≥10. "
                            "−0.6 if any security ticket or CBE ≥3. −0.5 if bugs ≥20. "
                            "Shown on Live sources and Live pulse. Not blended into exit pillars today."
                        ),
                        "green": "Score ≥ 4.0 (few CBE/security, bugs <20)",
                        "amber": "3.0–3.9, or Jira not connected",
                        "red": "< 3.0, or the Jira API failed",
                    },
                    {
                        "criterion": "Roadie — catalog scorecards",
                        "mechanism": (
                            "Tech Insights pass/fail per scorecard, mapped to handover areas by title. "
                            "Score = (passed / total) × 5. Matching areas: " + BLEND_55 + " "
                            "If no area match: 60% local overall + 40% Roadie average."
                        ),
                        "green": "Catalog checks average ≥ 4.0 (~80% passing)",
                        "amber": "3.0–3.9, or token missing",
                        "red": "< 3.0",
                    },
                    {
                        "criterion": "Confluence / Celospace — docs, SOPs, runbooks",
                        "mechanism": (
                            "Reads https://celonis-confluence.atlassian.net space DKB under the Task Mining hub "
                            "(page 17674883 and descendants). Same Atlassian token as Jira. "
                            "Each page is strong (≥700 chars), thin, or stub (WIP/TODO/empty). "
                            "Headline is SOP/docs quality (not averaged with a single thin SLO page). "
                            "SOPs/how-tos/handover pages → Knowledge (40% git + 30% Drive + 30% Confluence when Drive is on; "
                            "else 55% git + 45% Confluence). Named runbook/SLO pages blend into Operate only if there are ≥2 or they score ≥3.0. "
                            "Architecture pages blend the same way. "
                            "+0.4 substantial handover page, +0.3 onboarding checklist, +0.3 if ≥25% of pages were edited in the window, "
                            "−0.5 if everything is older than a year."
                        ),
                        "green": "Quality ≥ 4.0 (mostly strong, current SOPs + a real handover page)",
                        "amber": "3.0–3.9, or Confluence not connected",
                        "red": "< 3.0 (thin/stale wiki). P0 only if < 2.0",
                    },
                    {
                        "criterion": "Google Drive — KT pack",
                        "mechanism": (
                            "Task Mining squad Drive via Drive for Desktop. Recordings ≥20MB in handover/onboarding "
                            "folders: ≥8 → 5.0, ≥3 → 4.0, ≥1 → 3.0, other videos → 1.5. "
                            "Docs: +1.2 Engineering Handover, +0.8 Components Handover, +0.6 if ≥5 substantial docs, "
                            "−0.8 empty named docs. Quality = 55% recordings + 45% docs. Blend into Knowledge: " + BLEND_55
                        ),
                        "green": "Quality ≥ 4.0 (typically ≥3 KT recordings plus real handover docs)",
                        "amber": "3.0–3.9, or Drive not cached",
                        "red": "< 3.0",
                    },
                    {
                        "criterion": "Runbook quality",
                        "mechanism": (
                            "Classifies docs/runbooks, docs/ops, docs/on-call: stub (TODO/FIXME/empty), thin "
                            "(<700 chars or <3 steps), or strong (≥3 steps and a recovery action, or long + ≥4 steps). "
                            "If live Datadog monitor count is known, named-alert coverage below 80% cannot stay green "
                            "(quality is scaled down). Blend into Operate: 55% prior operate + 45% quality."
                        ),
                        "green": "Mostly strong pages and ≥80% monitor coverage",
                        "amber": "Mix of thin/strong, or coverage <80%",
                        "red": "Mostly stubs / thin, or quality < 3.0 (P0 when stubs dominate)",
                        "p0": True,
                    },
                    {
                        "criterion": "Code Purple (Celocore)",
                        "mechanism": (
                            "Git: purple verification lane + quality-prioritization / STEP. "
                            "Live: numeric KPIs from the Celocore Studio view, averaged to 0–5 "
                            "(percents ÷20, ratios ×5). Blend: " + BLEND_55 + " Incomplete on tier-1 is P0."
                        ),
                        "green": "Adopted in git and live ≥ 4.0",
                        "amber": "Partial lane, or live 3.0–3.9",
                        "red": "Missing on a production service, or live < 3.0",
                        "p0": True,
                    },
                    {
                        "criterion": "Kargo / RepoDepot",
                        "mechanism": (
                            "Adopted if catalog annotation repo-depot.celonis.dev/deploy-strategy is kargo. "
                            "Partial if RepoDepot/Kargo traces exist without the annotation. "
                            "Missing on a cloud service is P0. On-prem / library is N/A (green)."
                        ),
                        "green": "Adopted, or N/A",
                        "amber": "Traces without the annotation",
                        "red": "Cloud service with neither (P0)",
                        "p0": True,
                    },
                ],
            },
            {
                "id": "pulse",
                "title": "Live pulse (counts only)",
                "intro": (
                    "These numbers are listed and linked so you can inspect the backlog. "
                    "They do not change the 0–5 formula except where the same source is already scored above "
                    "(Datadog Sev-1, Jira CBE/security/bugs)."
                ),
                "rows": [
                    {
                        "criterion": "GitHub test files / E2E files / coverage %",
                        "mechanism": "File counts on the default-branch tree; coverage % from the latest coverage/cobertura/lcov check-run if one publishes a percent.",
                        "green": "Informational",
                        "amber": "— if no coverage check-run",
                        "red": "—",
                    },
                    {
                        "criterion": "Merged / reverted / regression PRs",
                        "mechanism": "GitHub Search in the window (merged:>=since, plus revert / regression keywords). Linked to the filtered PR list.",
                        "green": "Informational (release score uses review %, merge time, cadence — not these counts)",
                        "amber": "—",
                        "red": "—",
                    },
                    {
                        "criterion": "Jira CBE / security / features / bugs",
                        "mechanism": "Approximate-count API on TMT + CBE. Same queries as the Jira live score.",
                        "green": "See Jira live source",
                        "amber": "See Jira live source",
                        "red": "See Jira live source",
                    },
                    {
                        "criterion": "Confluence pages / SOPs / runbooks / handover / updated",
                        "mechanism": "Counts from the Task Mining Celospace hub. Same pages that feed the Confluence live score.",
                        "green": "See Confluence live source",
                        "amber": "See Confluence live source",
                        "red": "See Confluence live source",
                    },
                    {
                        "criterion": "Datadog P0 / alerts / monitors / incidents",
                        "mechanism": "Sev-1 count in the window, monitors currently in Alert, all tagged monitors, all incidents. Linked to Datadog filters.",
                        "green": "See Datadog live source",
                        "amber": "See Datadog live source",
                        "red": "Alerting monitors or Sev-1s pull operate red (P0)",
                    },
                ],
            },
        ],
    }
