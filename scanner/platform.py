from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from scanner.env import load_dotenv_files
from scanner.files import match_glob, read_text, rel

KARGO = re.compile(r"(?i)\bkargo\b")
CODE_PURPLE = re.compile(r"(?i)code[\s_-]*purple")
KARGO_BASE_DEFAULT = "https://kargo.us-2.celonis.cloud"
ROADIE_APP = "https://celonis.roadie.so"


def _ann(catalog: dict[str, Any], key: str) -> str:
    anns = catalog.get("annotations") or {}
    val = anns.get(key)
    return "" if val is None else str(val).strip()


def _is_on_prem(catalog: dict[str, Any]) -> bool:
    stype = str(catalog.get("type") or "").lower()
    return "on-prem" in stype or "onprem" in stype or stype in {"library", "website", "documentation"}


def _is_cloud_service(catalog: dict[str, Any]) -> bool:
    if _is_on_prem(catalog):
        return False
    stype = str(catalog.get("type") or "").lower()
    lifecycle = str(catalog.get("lifecycle") or "").lower()
    if stype in {"service", "backend"}:
        return True
    if catalog.get("annotations", {}).get("argocd/app-selector"):
        return True
    if lifecycle == "production" and stype == "service":
        return True
    return False


def _file_hits(root: Path, files: list[Path], patterns: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for path in match_glob(root, pattern, files, limit=20):
            key = rel(root, path)
            if key not in seen:
                seen.add(key)
                found.append(key)
    return found


def _content_search(
    root: Path,
    files: list[Path],
    glob_pat: str | list[str],
    regex: re.Pattern[str],
    limit: int = 8,
) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    patterns = glob_pat if isinstance(glob_pat, list) else [glob_pat]
    candidates: list[Path] = []
    seen: set[str] = set()
    for pattern in patterns:
        for path in match_glob(root, pattern, files, limit=80):
            key = str(path)
            if key not in seen:
                seen.add(key)
                candidates.append(path)
    for path in candidates:
        text = read_text(path)
        match = regex.search(text)
        if not match:
            continue
        hits.append({"file": rel(root, path), "snippet": match.group(0)[:80]})
        if len(hits) >= limit:
            break
    return hits


def assess_platform(root: Path, files: list[Path], catalog: dict[str, Any]) -> dict[str, Any]:
    kargo = _assess_kargo(root, files, catalog)
    purple = _assess_code_purple(root, files, catalog)
    gaps: list[dict[str, Any]] = []
    if kargo.get("gap"):
        gaps.append(kargo["gap"])
    if purple.get("gap"):
        gaps.append(purple["gap"])
    return {
        "kargo": kargo,
        "code_purple": purple,
        "gaps": gaps,
    }


def _kargo_urls(catalog: dict[str, Any]) -> dict[str, Any]:
    """Kargo project UI + RepoDepot Actions. Card title uses `url`."""
    load_dotenv_files()
    explicit = _ann(catalog, "handover.celonis.dev/kargo-url") or _ann(catalog, "kargo/project-url")
    for link in catalog.get("links") or []:
        title = str(link.get("title") or "").lower()
        href = str(link.get("url") or "").strip()
        if href and ("kargo" in title or "kargo" in href.lower()):
            explicit = explicit or href
    image = _ann(catalog, "repo-depot.celonis.dev/image-name")
    name = str(catalog.get("name") or "").strip()
    project = (image or name).replace("/", "-").strip("-")
    base = (os.environ.get("KARGO_BASE") or KARGO_BASE_DEFAULT).rstrip("/")
    kargo_ui = explicit or (f"{base}/project/{project}" if project else base)
    slug = _ann(catalog, "github.com/project-slug")
    actions = f"https://github.com/{slug}/actions?query=workflow%3Arepodepot" if slug else ""
    roadie = f"{ROADIE_APP}/catalog/default/component/{name}" if name else ""
    return {
        "url": kargo_ui,
        "links": {"kargo": kargo_ui, "actions": actions, "catalog": roadie},
        "signals_extra": {"project": project, "slug": slug},
    }


def _with_kargo_url(item: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    extra = _kargo_urls(catalog)
    item["url"] = extra["url"]
    item["links"] = extra["links"]
    signals = dict(item.get("signals") or {})
    signals.update(extra["signals_extra"])
    item["signals"] = signals
    return item


def _assess_kargo(root: Path, files: list[Path], catalog: dict[str, Any]) -> dict[str, Any]:
    strategy = _ann(catalog, "repo-depot.celonis.dev/deploy-strategy").lower()
    verification = _ann(catalog, "repo-depot.celonis.dev/deployment-verification").lower()
    argocd = _ann(catalog, "argocd/app-selector")
    kargo_hits = _content_search(
        root,
        files,
        [
            "catalog-info.yaml",
            "catalog-info.yml",
            ".github/**/*.yaml",
            ".github/**/*.yml",
            "docs/**/*.md",
        ],
        KARGO,
    )
    repo_depot = bool(
        any(k.startswith("repo-depot.celonis.dev/") for k in (catalog.get("annotations") or {}))
        or _file_hits(root, files, [".github/workflows/repodepot-*.yaml", ".github/workflows/repodepot-*.yml"])
    )
    evidence = []
    if strategy:
        evidence.append(f"catalog deploy-strategy={strategy}")
    if verification:
        evidence.append(f"deployment-verification={verification}")
    if argocd:
        evidence.append(f"argocd/app-selector={argocd}")
    evidence.extend(f"{h['file']}: {h['snippet']}" for h in kargo_hits[:4])

    if _is_on_prem(catalog):
        return _with_kargo_url(
            {
                "id": "kargo",
                "label": "Kargo adoption",
                "status": "na",
                "rag": "green",
                "title": "Not applicable",
                "summary": "On-prem / non-cloud component — Kargo promotion is not expected.",
                "evidence": evidence,
                "signals": {"strategy": strategy, "verification": verification, "mentions": len(kargo_hits)},
                "gap": None,
            },
            catalog,
        )

    if strategy == "kargo":
        extra = " STEP/deployment-verification is on." if verification in {"true", "yes", "1"} else " Deployment verification annotation is not set."
        return _with_kargo_url(
            {
                "id": "kargo",
                "label": "Kargo adoption",
                "status": "adopted",
                "rag": "green",
                "title": "Adopted",
                "summary": "catalog-info sets deploy-strategy: kargo." + extra,
                "evidence": evidence,
                "signals": {"strategy": strategy, "verification": verification, "mentions": len(kargo_hits)},
                "gap": None,
            },
            catalog,
        )

    if repo_depot or kargo_hits:
        return _with_kargo_url(
            {
                "id": "kargo",
                "label": "Kargo adoption",
                "status": "partial",
                "rag": "amber",
                "title": "In progress",
                "summary": "RepoDepot / Kargo traces exist, but catalog deploy-strategy is not `kargo`.",
                "evidence": evidence,
                "signals": {"strategy": strategy or "(unset)", "verification": verification, "mentions": len(kargo_hits)},
                "gap": {
                    "id": "platform.kargo",
                    "area": "release",
                    "title": "Kargo adoption incomplete",
                    "ask": "Finish Kargo migration: set repo-depot.celonis.dev/deploy-strategy: kargo and keep old-team backup until the new team can promote a RC.",
                    "p0": True,
                },
            },
            catalog,
        )

    if _is_cloud_service(catalog):
        return _with_kargo_url(
            {
                "id": "kargo",
                "label": "Kargo adoption",
                "status": "missing",
                "rag": "red",
                "title": "Not adopted",
                "summary": "Production cloud service with no Kargo deploy-strategy and no RepoDepot Kargo workflows.",
                "evidence": evidence,
                "signals": {"strategy": strategy or "(unset)", "verification": verification, "mentions": 0},
                "gap": {
                    "id": "platform.kargo",
                    "area": "release",
                    "title": "Kargo not adopted",
                    "ask": "Cloud production services are expected to be on Kargo. Fund migration before handover or accept residual deploy-path risk.",
                    "p0": True,
                },
            },
            catalog,
        )

    return _with_kargo_url(
        {
            "id": "kargo",
            "label": "Kargo adoption",
            "status": "na",
            "rag": "green",
            "title": "Not assessed",
            "summary": "No cloud-service catalog type; Kargo not required.",
            "evidence": evidence,
            "signals": {"strategy": strategy or "(unset)", "verification": verification, "mentions": len(kargo_hits)},
            "gap": None,
        },
        catalog,
    )


def _assess_code_purple(root: Path, files: list[Path], catalog: dict[str, Any]) -> dict[str, Any]:
    cq_files = _file_hits(
        root,
        files,
        [
            ".github/workflows/code-quality-prioritization.yaml",
            ".github/workflows/code-quality-prioritization.yml",
            ".github/workflows/code-quality-prioritization.yaml.yml",
            ".github/workflows/*code-quality*",
            ".github/workflows/*coverage*",
        ],
    )
    coverage_action = _content_search(
        root,
        files,
        [".github/workflows/*.yml", ".github/workflows/*.yaml", ".github/workflows/*.yaml.yml"],
        re.compile(r"coverage-relevance-action|code_quality_analysis"),
    )
    purple_tenants = _content_search(
        root,
        files,
        [
            "**/src/test/resources/*.yml",
            "**/src/test/resources/*.yaml",
            "docs/**/*.md",
            ".github/workflows/*.yml",
            ".github/workflows/*.yaml",
            "docs/testing/**/*.md",
        ],
        re.compile(r"(?i)[a-z0-9-]+-purple"),
    )
    named = _content_search(
        root,
        files,
        ["docs/**/*.md", "README.md", "catalog-info.yaml", ".github/**/*"],
        CODE_PURPLE,
    )
    step_hits = _content_search(
        root,
        files,
        [
            "**/src/test/resources/*.yml",
            "**/src/test/resources/*.yaml",
            "docs/**/*.md",
            "catalog-info.yaml",
        ],
        re.compile(r"(?i)purple lane|deployment-verification|STEP.*lane"),
    )
    sonar = _ann(catalog, "sonarqube.org/project-key")
    verification = _ann(catalog, "repo-depot.celonis.dev/deployment-verification").lower() in {"true", "yes", "1"}

    has_quality_job = bool(cq_files or coverage_action)
    has_purple_lane = bool(purple_tenants or named)
    has_step = bool(step_hits or verification)

    evidence: list[str] = []
    if sonar:
        evidence.append(f"Sonar project {sonar}")
    if verification:
        evidence.append("deployment-verification=true")
    evidence.extend(cq_files[:4])
    evidence.extend(f"{h['file']}: {h['snippet']}" for h in (named + purple_tenants + coverage_action)[:6])

    signals = {
        "quality_job": has_quality_job,
        "purple_lane": has_purple_lane,
        "step_or_verification": has_step,
        "sonar": bool(sonar),
    }

    if has_purple_lane and (has_quality_job or has_step):
        return {
            "id": "code_purple",
            "label": "Code Purple status",
            "status": "adopted",
            "rag": "green",
            "title": "Adopted",
            "summary": "Purple verification lane is configured and quality/STEP signals are present.",
            "evidence": evidence,
            "signals": signals,
            "url": "https://celocore.us-2.celonis.cloud/package-manager/ui/views/ui/spaces/f88d799c-2272-4a3c-8335-29c05d37acdf/packages/cd114198-b0ef-431b-ad0b-b19455502e5f/nodes/ad1b7411-7f8a-47c8-980e-2dae22373407?activeTabs=code-purple-metrics:5655bef6-d3fe-4e48-85bb-cdddaffa09ce",
            "gap": None,
        }

    if has_quality_job or has_purple_lane or has_step or sonar:
        missing = []
        if not has_purple_lane:
            missing.append("no *-purple tenant/lane")
        if not has_quality_job:
            missing.append("no code-quality-prioritization workflow")
        if not has_step:
            missing.append("no STEP / deployment-verification")
        return {
            "id": "code_purple",
            "label": "Code Purple status",
            "status": "partial",
            "rag": "amber",
            "title": "Partial",
            "summary": "Some Code Purple signals exist (" + ", ".join(k for k, v in signals.items() if v) + "). Gaps: " + ", ".join(missing) + ".",
            "evidence": evidence,
            "signals": signals,
            "gap": {
                "id": "platform.code_purple",
                "area": "release",
                "title": "Code Purple incomplete",
                "ask": "Complete the purple verification lane (tenant + STEP/deployment-verification + coverage-relevance job) so the new team inherits a known quality bar.",
                "p0": str(catalog.get("tier") or "") in {"1", "'1'"},
            },
        }

    p0 = _is_cloud_service(catalog) or str(catalog.get("tier") or "") in {"1", "2", "'1'", "'2'"}
    return {
        "id": "code_purple",
        "label": "Code Purple status",
        "status": "missing",
        "rag": "red",
        "title": "Not started",
        "summary": "No purple tenant, no code-quality-prioritization workflow, no STEP/verification annotation.",
        "evidence": evidence,
        "signals": signals,
        "gap": {
            "id": "platform.code_purple",
            "area": "release",
            "title": "Code Purple not started",
            "ask": "Stand up Code Purple (quality-prioritization workflow + purple verification lane) before the new team takes production ownership.",
            "p0": p0,
        },
    }
