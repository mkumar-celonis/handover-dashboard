from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from scanner.datadog import apply_datadog, assess_datadog
from scanner.roadie import apply_roadie, assess_roadie
from scanner.github import apply_github, assess_github
from scanner.jira import assess_jira
from scanner.celonis import apply_code_purple_live, assess_code_purple_metrics
from scanner.files import iter_files, read_text
from scanner.platform import assess_platform
from scanner.handover import apply_handover
from scanner.runbooks import apply_runbooks, assess_runbooks
from scanner.drive import apply_drive, assess_drive
from scanner.confluence import apply_confluence, assess_confluence
from scanner.groups import (
    attach_local_paths,
    fetch_group_services,
    is_group_query,
    resolve_service_path,
    workspace_roots,
)
from scanner.score import check_to_dict, load_config, run_check, score_report

CONFIG_PATH = Path(__file__).parent / "checks.yaml"


def git_meta(root: Path) -> dict[str, str]:
    meta = {"branch": "", "remote": "", "head": ""}

    def git(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            return (out.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""

    meta["branch"] = git("rev-parse", "--abbrev-ref", "HEAD")
    meta["remote"] = git("config", "--get", "remote.origin.url")
    if not meta["remote"]:
        for line in git("remote", "-v").splitlines():
            parts = line.split()
            if len(parts) >= 2 and "github" in parts[1]:
                meta["remote"] = parts[1]
                break
            if len(parts) >= 2 and not meta["remote"]:
                meta["remote"] = parts[1]
    meta["head"] = git("log", "-1", "--format=%h %s")
    return meta


def parse_catalog(root: Path) -> dict[str, Any]:
    for name in ("catalog-info.yaml", "catalog-info.yml"):
        path = root / name
        if not path.is_file():
            continue
        try:
            import yaml

            docs = list(yaml.safe_load_all(read_text(path)))
        except Exception:
            return {"file": name}
        info: dict[str, Any] = {"file": name}
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") != "Component":
                continue
            meta = doc.get("metadata") or {}
            spec = doc.get("spec") or {}
            info.update(
                {
                    "name": meta.get("name"),
                    "title": meta.get("title"),
                    "description": meta.get("description"),
                    "owner": spec.get("owner"),
                    "lifecycle": spec.get("lifecycle"),
                    "tier": spec.get("tier"),
                    "type": spec.get("type"),
                    "dependsOn": spec.get("dependsOn") or [],
                    "providesApis": spec.get("providesApis") or [],
                    "annotations": meta.get("annotations") or {},
                    "links": [
                        {"title": link.get("title"), "url": link.get("url"), "type": link.get("type")}
                        for link in (meta.get("links") or [])
                        if isinstance(link, dict)
                    ],
                }
            )
            break
        return info
    return {}


def scan_repo(
    root: Path,
    config_path: Path = CONFIG_PATH,
    signoff: dict[str, Any] | None = None,
    window_days: int = 90,
    weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Not a directory: {root}")
    days = int(window_days or 90)
    if days <= 0:
        days = 365
    days = min(max(days, 1), 400)
    config = load_config(config_path)
    files = [Path(p) for p in iter_files(root)]
    results = [run_check(root, spec, files) for spec in config["checks"]]
    report = score_report(config, results)
    catalog = parse_catalog(root)
    git = git_meta(root)
    name = catalog.get("title") or catalog.get("name") or root.name
    platform = assess_platform(root, files, catalog)
    purple_live = assess_code_purple_metrics(catalog)
    platform["code_purple"] = apply_code_purple_live(platform.get("code_purple") or {}, purple_live)
    report = apply_platform(report, platform)
    live = assess_datadog(catalog, window_days=days)
    report = apply_datadog(report, live)
    github = assess_github(git, window_days=days, catalog=catalog)
    report = apply_github(report, github)
    jira = assess_jira(catalog, service_id=catalog.get("name") or root.name, window_days=days)
    roadie = assess_roadie(catalog)
    report = apply_roadie(report, roadie)
    runbooks = assess_runbooks(root, files, live)
    report = apply_runbooks(report, runbooks)
    drive = assess_drive(catalog, root.name)
    report = apply_drive(report, drive)
    confluence = assess_confluence(catalog, window_days=days)
    report = apply_confluence(report, confluence)
    report = apply_handover(
        report, signoff, root=root, catalog=catalog, config=config, ui_weights=weights
    )
    return {
        "repo": {
            "path": str(root),
            "name": name,
            "git": git,
            "catalog": catalog,
            "file_count": len(files),
        },
        **report,
        "window_days": days,
        "platform": platform,
        "datadog": live,
        "github": github,
        "jira": jira,
        "roadie": roadie,
        "runbooks": runbooks,
        "drive": drive,
        "confluence": confluence,
        "checks": [check_to_dict(r) for r in results],
        "service_id": catalog.get("name") or root.name,
    }


def scan_catalog_only(
    member: dict[str, Any],
    signoff: dict[str, Any] | None = None,
    window_days: int = 90,
    weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    days = min(max(int(window_days or 90), 1), 400)
    config = load_config(CONFIG_PATH)
    catalog = member.get("catalog") or {"name": member.get("id")}
    git = {
        "branch": "",
        "remote": member.get("remote") or "",
        "slug": member.get("github_slug") or "",
        "service_id": member.get("id") or catalog.get("name") or "",
        "head": "",
    }
    report = score_report(config, [])
    live = assess_datadog(catalog, window_days=days)
    report = apply_datadog(report, live)
    github = assess_github(git, window_days=days, catalog=catalog)
    report = apply_github(report, github)
    jira = assess_jira(catalog, service_id=member.get("id") or catalog.get("name"), window_days=days)
    roadie = assess_roadie(catalog)
    report = apply_roadie(report, roadie)
    confluence = assess_confluence(catalog, window_days=days)
    report = apply_confluence(report, confluence)
    report = apply_handover(report, signoff, catalog=catalog, config=config, ui_weights=weights)
    name = catalog.get("title") or catalog.get("name") or member.get("id")
    return {
        "repo": {
            "path": "",
            "name": name,
            "git": git,
            "catalog": catalog,
            "file_count": 0,
        },
        **report,
        "window_days": days,
        "platform": {},
        "datadog": live,
        "github": github,
        "jira": jira,
        "roadie": roadie,
        "runbooks": {"status": "skipped", "title": "No local clone", "rag": "amber"},
        "drive": {"status": "skipped"},
        "confluence": confluence,
        "checks": [],
        "service_id": member.get("id"),
        "missing_clone": True,
    }


def _scan_group_member(
    member: dict[str, Any],
    window_days: int,
    tune: dict[str, Any] | None,
) -> dict[str, Any]:
    tune = tune or {}
    signoff = tune.get("signoff")
    weights = tune.get("weights")
    path = (tune.get("path") or member.get("path") or "").strip()
    try:
        if path and Path(path).is_dir():
            report = scan_repo(Path(path), signoff=signoff, window_days=window_days, weights=weights)
        else:
            report = scan_catalog_only(member, signoff=signoff, window_days=window_days, weights=weights)
        report["service_id"] = member.get("id") or report.get("service_id")
        report["service_title"] = member.get("title") or (report.get("repo") or {}).get("name")
        report["service_url"] = member.get("url")
        report["service_type"] = member.get("type")
        return report
    except Exception as exc:
        return {
            "service_id": member.get("id"),
            "service_title": member.get("title") or member.get("id"),
            "service_url": member.get("url"),
            "error": str(exc)[:400],
            "overall_pct": 0,
            "overall_rag": "red",
            "verdict": {"verdict": "block", "label": "Scan failed", "detail": str(exc)[:300]},
            "handover": {"points": 0, "max": 100},
            "repo": {"path": path, "name": member.get("title") or member.get("id"), "catalog": member.get("catalog") or {}, "git": {}},
        }


def scan_service(
    service_id: str,
    *,
    path: str | None = None,
    title: str | None = None,
    url: str | None = None,
    service_type: str | None = None,
    github_slug: str | None = None,
    catalog: dict[str, Any] | None = None,
    window_days: int = 90,
    workspace: str | None = None,
    signoff: dict[str, Any] | None = None,
    weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from scanner.github import KNOWN_SLUGS

    catalog = catalog or {"name": service_id, "title": title or service_id, "type": service_type or "service"}
    slug = (
        (github_slug or "").strip()
        or (catalog.get("annotations") or {}).get("github.com/project-slug")
        or KNOWN_SLUGS.get(service_id)
        or ""
    )
    member = {
        "id": service_id,
        "title": title or service_id,
        "url": url,
        "type": service_type or "service",
        "path": (path or "").strip(),
        "github_slug": slug,
        "remote": f"https://github.com/{slug}.git" if slug else "",
        "catalog": catalog,
    }
    if not member["path"]:
        member["path"] = resolve_service_path(service_id, workspace_roots(workspace))
    return _scan_group_member(
        member,
        window_days,
        {"path": member["path"], "signoff": signoff, "weights": weights},
    )


def scan_group(
    group: str = "group:default/task-mining",
    window_days: int = 90,
    workspace: str | None = None,
    services: dict[str, Any] | None = None,
    selected: list[str] | None = None,
) -> dict[str, Any]:
    info = attach_local_paths(fetch_group_services(group), workspace)
    tunes = services or {}
    members = info.get("services") or []
    if selected is not None:
        wanted = {str(x) for x in selected}
        members = [m for m in members if m.get("id") in wanted]
    reports: list[dict[str, Any]] = []
    if members:
        workers = min(4, len(members))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_scan_group_member, member, window_days, tunes.get(member["id"])): member["id"]
                for member in members
            }
            by_id: dict[str, dict[str, Any]] = {}
            for fut in as_completed(futs):
                by_id[futs[fut]] = fut.result()
        reports = [by_id[m["id"]] for m in members if m["id"] in by_id]
    scored = [r for r in reports if not r.get("error")]
    points = [int((r.get("handover") or {}).get("points") or r.get("overall_pct") or 0) for r in scored]
    avg = int(round(sum(points) / len(points))) if points else 0
    ranks = {"block": 0, "conditional": 1, "go": 2}
    worst = "block"
    if scored:
        worst = min(
            (str((r.get("verdict") or {}).get("verdict") or "block") for r in scored),
            key=lambda v: ranks.get(v, 0),
        )
    labels = {"block": "Block handover", "conditional": "Soft handover", "go": "Complete handover"}
    failing = [r.get("service_title") or r.get("service_id") for r in reports if (r.get("verdict") or {}).get("verdict") == "block" or r.get("error")]
    if worst == "go":
        detail = f"{len(scored)} services in {info['title']} all pass their own scorecards."
        action = "Sign group cutover when people sign-off is complete on every service."
    elif worst == "conditional":
        detail = f"Group average {avg}/100. One or more services still need a 2-week bridge."
        action = "Close the weakest service scorecards before the group leaves outgoing support."
    else:
        why = ", ".join(failing) if failing else "one or more services are below the bar"
        detail = f"Group average {avg}/100. Blocked by {why}."
        action = "Do not transfer the squad until every service scorecard is at least soft-handover."
    return {
        "kind": "group",
        "group": {
            "id": info["id"],
            "ref": info["ref"],
            "title": info["title"],
            "url": info["url"],
            "source": info["source"],
            "service_count": len(members),
        },
        "window_days": window_days,
        "overall_pct": avg,
        "overall_rag": {"block": "red", "conditional": "amber", "go": "green"}[worst],
        "verdict": {
            "verdict": worst,
            "label": labels[worst],
            "detail": detail,
            "action": action,
        },
        "services": reports,
        "handover": {"points": avg, "max": 100},
    }


def apply_platform(report: dict[str, Any], platform: dict[str, Any]) -> dict[str, Any]:
    """Merge Kargo / Code Purple into gaps, P0 lists, plan, and verdict."""
    from scanner.score import decision

    extra = platform.get("gaps") or []
    existing_ids = {g.get("id") for g in report.get("gaps") or []}
    for gap in extra:
        if gap.get("id") not in existing_ids:
            report.setdefault("gaps", []).append(gap)
    for item in (platform.get("kargo"), platform.get("code_purple")):
        if not item:
            continue
        if item.get("status") == "missing" and (item.get("gap") or {}).get("p0"):
            if item["id"] not in report.get("p0_reds", []):
                report.setdefault("p0_reds", []).append(item["id"])
        elif item.get("status") == "partial" and (item.get("gap") or {}).get("p0"):
            if item["id"] not in report.get("p0_ambers", []):
                report.setdefault("p0_ambers", []).append(item["id"])
    verdict = decision(report["overall_pct"], report.get("p0_reds") or [], report.get("p0_ambers") or [])
    report["verdict"] = verdict
    report["overall_rag"] = {"block": "red", "conditional": "amber", "go": "green"}[verdict["verdict"]]
    if any(g.get("id", "").startswith("platform.") for g in extra):
        report.setdefault("plan", [])
        already = any("Kargo" in row.get("outcome", "") or "Code Purple" in row.get("outcome", "") for row in report["plan"])
        if not already:
            report["plan"].insert(
                1,
                {
                    "week": "1–2",
                    "outcome": "Close platform-program gaps: finish Kargo adoption and Code Purple (purple lane + quality job + STEP/verification).",
                    "maps_to": "Kargo / Code Purple",
                },
            )
    return report


def to_markdown(report: dict[str, Any]) -> str:
    repo = report["repo"]
    catalog = repo.get("catalog") or {}
    v = report["verdict"]
    lines = [
        f"# Handover readiness: {repo['name']}",
        "",
        f"**Path:** `{repo['path']}`  ",
        f"**Git:** {repo['git'].get('branch', '')} · {repo['git'].get('head', '')}  ",
        f"**Verdict:** {v['label']} · **{report['overall_pct']}/{(report.get('handover') or {}).get('max') or 100}** · {report['overall_rag'].upper()}",
        "",
        v["detail"],
        "",
    ]
    if v.get("action"):
        lines += [f"**Action:** {v['action']}", ""]
    pillars = report.get("pillars") or []
    if pillars:
        lines += [
            "## Exit scorecard",
            "",
            "| Pillar | Score | Pass | RAG |",
            "|---|---|---|---|",
        ]
        for p in pillars:
            flag = " below pass" if p.get("below_pass") else ""
            lines.append(
                f"| {p['label']} | {p['earned']}/{p['max']} | {p['pass']} | {p['rag']}{flag} |"
            )
        ho = report.get("handover") or {}
        weights = ho.get("weights") or {}
        if weights:
            lines.append(
                "- Weighted by risk, not evenly: "
                + ", ".join(f"{k} {v}" for k, v in weights.items())
                + f" (source: {ho.get('weights_source') or 'default'})."
            )
        green = (ho.get("thresholds") or {}).get("green", 85)
        amber = (ho.get("thresholds") or {}).get("amber", 70)
        lines += [
            "",
            f"Green ≥{green} with all pillars passing. Amber {amber}–{green - 1} (outgoing stays 2 weeks). Below {amber} or no shadowing → block.",
            "",
        ]

    if catalog:
        lines += [
            "## Service",
            "",
            f"- Owner: `{catalog.get('owner')}`",
            f"- Lifecycle: {catalog.get('lifecycle')} · type: {catalog.get('type')} · tier: {catalog.get('tier')}",
            f"- {catalog.get('description') or ''}",
            "",
        ]
        deps = catalog.get("dependsOn") or []
        if deps:
            lines.append("Depends on: " + ", ".join(f"`{d}`" for d in deps[:20]))
            lines.append("")
    platform = report.get("platform") or {}
    if platform:
        lines += ["## Platform programs", ""]
        for key in ("kargo", "code_purple"):
            item = platform.get(key) or {}
            if not item:
                continue
            lines.append(
                f"- **{item.get('label')}:** {item.get('title')} ({item.get('rag', '').upper()}) — {item.get('summary')}"
            )
            if item.get("url"):
                lines.append(f"  - {item['url']}")
            if item.get("score") is not None:
                lines.append(f"  - Score: {item.get('score')}/5")
            for line in (item.get("why") or [])[:5]:
                lines.append(f"  - {line}")
            for ev in (item.get("evidence") or [])[:5]:
                lines.append(f"  - `{ev}`")
        lines.append("")
    github = report.get("github") or {}
    if github:
        lines += [
            "## GitHub (live)",
            "",
            f"- **{github.get('title')}** ({str(github.get('rag') or '').upper()}) — {github.get('summary')}",
        ]
        if github.get("repo"):
            lines.append(f"- Repo: `{github['repo']}`")
        if github.get("url"):
            lines.append(f"- {github['url']}")
        if github.get("window_days"):
            lines.append(f"- Window: {github.get('window_label') or github['window_days']} days")
        for line in github.get("why") or []:
            lines.append(f"  - {line}")
        for setup in github.get("setup") or []:
            lines.append(f"  - {setup}")
        lines.append("")
    live = report.get("datadog") or {}
    if live:
        lines += [
            "## Datadog (live)",
            "",
            f"- **{live.get('title')}** ({str(live.get('rag') or '').upper()}) — {live.get('summary')}",
        ]
        if live.get("service"):
            lines.append(f"- Monitor tag: `{live['service']}`")
        for line in live.get("why") or []:
            lines.append(f"  - {line}")
        sig = live.get("signals") or {}
        if sig:
            lines.append(
                f"- Monitors: {sig.get('monitors', 0)} (Alert {sig.get('alerting', 0)}, Warn {sig.get('warn', 0)}) · "
                f"SLOs: {sig.get('slos', 0)} · Incidents 90d: {sig.get('incidents_90d', 0)} (Sev-1 {sig.get('sev1_90d', 0)})"
            )
        for mon in (live.get("monitors") or [])[:8]:
            if mon.get("status") in {"Alert", "Warn"}:
                lines.append(f"  - [{mon.get('status')}] {mon.get('name')} ({mon.get('url')})")
        for setup in live.get("setup") or []:
            lines.append(f"  - {setup}")
        lines.append("")
    roadie = report.get("roadie") or {}
    if roadie:
        lines += [
            "## Roadie scorecards",
            "",
            f"- **{roadie.get('title')}** ({str(roadie.get('rag') or '').upper()}) — {roadie.get('summary')}",
        ]
        if roadie.get("entity"):
            lines.append(f"- Entity: `{roadie['entity']}`")
        if roadie.get("url"):
            lines.append(f"- Catalog: {roadie['url']}")
        for line in roadie.get("why") or []:
            lines.append(f"  - {line}")
        for card in roadie.get("scorecards") or []:
            pct = f"{card.get('percent')}%" if card.get("percent") is not None else "n/a"
            lines.append(
                f"  - {card.get('title')}: {card.get('passed', 0)} pass / {card.get('failed', 0)} fail ({pct})"
                + (f" → {card.get('area')}" if card.get("area") else "")
            )
        for setup in roadie.get("setup") or []:
            lines.append(f"  - {setup}")
        lines.append("")
    runbooks = report.get("runbooks") or {}
    if runbooks:
        lines += [
            "## Runbook quality",
            "",
            f"- **{runbooks.get('title')}** ({str(runbooks.get('rag') or '').upper()}) — {runbooks.get('score')}/5",
        ]
        for finding in runbooks.get("findings") or []:
            lines.append(f"- {finding}")
        lines.append("")
    drive = report.get("drive") or {}
    if drive:
        lines += [
            "## Google Drive (docs & recordings)",
            "",
            f"- **{drive.get('title')}** ({str(drive.get('rag') or '').upper()}) — {drive.get('score')}/5 — {drive.get('summary')}",
        ]
        if drive.get("url"):
            lines.append(f"- Folder: {drive['url']}")
        for finding in drive.get("findings") or []:
            lines.append(f"- {finding}")
        lines.append("")
    lines += [
        "## Scorecard",
        "",
        "| Area | Weight | P0 | Score | RAG | Why |",
        "|---|---|---|---|---|---|",
    ]
    for area in report["areas"]:
        p0 = "Yes" if area["p0"] else "No"
        lines.append(
            f"| {area['label']} | {int(area['weight']*100)}% | {p0} | {area['score']}/5 | {area['rag']} | {area['why']} |"
        )
    lines += ["", "## P0 / escalation gaps", ""]
    gaps = report.get("gaps") or []
    if not gaps:
        lines.append("No automated escalation gaps (missing checks with escalate_if_missing).")
    else:
        for gap in gaps:
            tag = "P0" if gap.get("p0") else "P1"
            lines.append(f"- **[{tag}] {gap['title']}** — {gap['ask']}")
    lines += ["", "## Close-the-gap plan", ""]
    for row in report.get("plan") or []:
        lines.append(f"- **Week {row['week']}** ({row['maps_to']}): {row['outcome']}")
    lines += ["", "## Limits", ""]
    for limit in report.get("limits") or []:
        lines.append(f"- {limit}")
    lines.append("")
    return "\n".join(lines)
