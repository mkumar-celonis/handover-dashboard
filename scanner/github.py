from __future__ import annotations

import json
import os
import re
import statistics
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from scanner.env import load_dotenv_files
from scanner.score import rag_from_score

API_DEFAULT = "https://api.github.com"
TOKEN_HELP = "https://github.com/settings/tokens"

# Catalog name → GitHub slug when origin is missing (group / live-only scans).
KNOWN_SLUGS = {
    "task-mining": "celonis/cloud-task-mining",
    "cloud-task-mining": "celonis/cloud-task-mining",
    "task-mining-ai": "celonis/task-mining-ai",
    "task-mining-uploader": "celonis/task-mining-uploader",
    "tm-image-collector": "celonis/tm-image-collector",
    "task-mining-gateway": "celonis/task-mining-gateway",
}


def credentials() -> dict[str, str] | None:
    load_dotenv_files()
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        return None
    base = (os.environ.get("GITHUB_API_BASE") or API_DEFAULT).strip().rstrip("/")
    timeout = str(float(os.environ.get("HTTP_TIMEOUT", "45")))
    return {"token": token, "api_base": base, "timeout": timeout}


def repo_slug(remote: str) -> str:
    text = (remote or "").strip()
    if text.endswith(".git"):
        text = text[:-4]
    text = text.rstrip("/")
    if "github.com:" in text:
        return text.split("github.com:", 1)[1].strip("/")
    match = re.search(r"github\.com(?:[-][\w.]+)?[/:]([^/]+/[^/#?]+)", text)
    if match:
        return match.group(1).rstrip("/")
    # SSH host alias: git@github.com-celonis:org/repo
    match = re.search(r"github\.com[-_][\w.-]+:([^/]+/[^/#?]+)", text)
    if match:
        return match.group(1).rstrip("/")
    if text.count("/") == 1 and ":" not in text and not text.startswith("http"):
        return text
    return ""


def resolve_slug(
    git: dict[str, Any] | None = None,
    catalog: dict[str, Any] | None = None,
    extra: str | None = None,
) -> str:
    """Prefer origin, then catalog annotation, then known Task Mining slugs."""
    git = git or {}
    for candidate in (
        extra,
        git.get("slug"),
        git.get("remote"),
        git.get("github_slug"),
    ):
        slug = repo_slug(str(candidate or ""))
        if slug:
            return slug
    anns = (catalog or {}).get("annotations") or {}
    for key in ("github.com/project-slug", "github.com/projectSlug"):
        slug = repo_slug(str(anns.get(key) or ""))
        if slug:
            return slug
    name = str((catalog or {}).get("name") or git.get("service_id") or "").strip()
    mapped = KNOWN_SLUGS.get(name) or KNOWN_SLUGS.get(name.replace("_", "-"))
    return mapped or ""


def _headers(creds: dict[str, str]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {creds['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "handover-dashboard",
    }


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
        err_body = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"GitHub HTTP {exc.code} on {path}: {err_body}") from exc


def _get_allow(creds: dict[str, str], path: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"{creds['api_base']}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers=_headers(creds), method="GET")
    timeout = float(creds.get("timeout") or 45)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:200]
        try:
            parsed = json.loads(err_body) if err_body else {}
        except json.JSONDecodeError:
            parsed = {"message": err_body}
        return exc.code, parsed


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _window_label(days: int) -> str:
    if days == 7:
        return "1 week"
    if days == 14:
        return "2 weeks"
    if days == 30:
        return "1 month"
    if days % 30 == 0 and days >= 30:
        months = days // 30
        return f"{months} month" if months == 1 else f"{months} months"
    return f"{days} days"


def _fetch_repo(creds: dict[str, str], slug: str, days: int) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
    info_code, info = _get_allow(creds, f"/repos/{slug}")
    if info_code >= 400:
        raise RuntimeError(f"GitHub HTTP {info_code} on /repos/{slug}: {info}")
    branch = str((info or {}).get("default_branch") or "main")
    html_url = str((info or {}).get("html_url") or f"https://github.com/{slug}")

    open_prs_payload = _get(creds, f"/repos/{slug}/pulls", {"state": "open", "per_page": 100})
    open_prs = len(open_prs_payload) if isinstance(open_prs_payload, list) else 0

    hrs: list[float] = []
    merged_nums: list[int] = []
    page = 1
    while page <= 3 and len(hrs) < 80:
        prs = _get(
            creds,
            f"/repos/{slug}/pulls",
            {"state": "closed", "per_page": 50, "page": page, "sort": "updated", "direction": "desc"},
        )
        if not isinstance(prs, list) or not prs:
            break
        past_cutoff = False
        for pr in prs:
            merged_at = pr.get("merged_at")
            if not merged_at:
                continue
            merged = _parse_dt(merged_at)
            if merged < cutoff:
                past_cutoff = True
                continue
            created = _parse_dt(pr.get("created_at") or merged_at)
            hrs.append((merged - created).total_seconds() / 3600)
            if pr.get("number") is not None:
                merged_nums.append(int(pr["number"]))
        if past_cutoff:
            break
        page += 1

    reviewed = 0
    for number in merged_nums[:15]:
        code, reviews = _get_allow(creds, f"/repos/{slug}/pulls/{number}/reviews", {"per_page": 20})
        if code == 200 and isinstance(reviews, list) and reviews:
            reviewed += 1
    sample = min(len(merged_nums), 15)
    reviewed_pct = round(reviewed / sample * 100) if sample else 0

    prot_code, _prot = _get_allow(creds, f"/repos/{slug}/branches/{urllib.parse.quote(branch)}/protection")
    if prot_code == 200:
        protected: bool | None = True
    elif prot_code == 403:
        protected = None
    else:
        protected = False

    commits = _get(creds, f"/repos/{slug}/commits", {"since": cutoff.isoformat(), "per_page": 100})
    commits_count = len(commits) if isinstance(commits, list) else 0
    weeks = max(days / 7, 1)
    commits_per_week = round(commits_count / weeks, 1)

    rel_code, releases = _get_allow(creds, f"/repos/{slug}/releases", {"per_page": 50})
    rel_list = releases if rel_code == 200 and isinstance(releases, list) else []
    releases_in_window = 0
    for rel in rel_list:
        published = rel.get("published_at") or rel.get("created_at")
        if not published:
            continue
        if _parse_dt(published) >= cutoff:
            releases_in_window += 1
    releases_per_week = round(releases_in_window / weeks, 2)
    if releases_per_week >= 1:
        deploy = "high"
    elif releases_per_week >= 0.25:
        deploy = "medium"
    else:
        deploy = "low"

    metrics = {
        "repo": slug,
        "url": html_url,
        "default_branch": branch,
        "open_prs": open_prs,
        "merged_prs_sampled": len(hrs),
        "median_merge_hours": round(statistics.median(hrs), 1) if hrs else 0,
        "reviewed_pct": reviewed_pct,
        "review_sample": sample,
        "branch_protection": protected,
        "commits_per_week": commits_per_week,
        "releases_per_week": releases_per_week,
        "deployment_frequency": deploy,
    }
    try:
        metrics.update(_fetch_quality(creds, slug, branch, days, cutoff))
    except Exception:
        pass
    return metrics


TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|__tests__)(/|$)|(^|/)test_[^/]+|_test\.\w+$|\.(spec|test)\.(ts|tsx|js|jsx|py|go|java)$",
    re.I,
)
E2E_FILE_RE = re.compile(r"(e2e|playwright|cypress|selenium)", re.I)
COV_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _search_pr_count(creds: dict[str, str], query: str) -> int | None:
    code, data = _get_allow(creds, "/search/issues", {"q": query, "per_page": 1})
    if code != 200 or not isinstance(data, dict):
        return None
    try:
        return int(data.get("total_count") or 0)
    except (TypeError, ValueError):
        return None


def _fetch_quality(creds: dict[str, str], slug: str, branch: str, days: int, cutoff: datetime) -> dict[str, Any]:
    since = cutoff.date().isoformat()
    merged = _search_pr_count(creds, f"repo:{slug} is:pr is:merged merged:>={since}")
    reverted = _search_pr_count(creds, f"repo:{slug} is:pr is:merged revert merged:>={since}")
    regression = _search_pr_count(creds, f"repo:{slug} is:pr is:merged regression merged:>={since}")

    test_files = 0
    e2e_files = 0
    tree_code, tree = _get_allow(creds, f"/repos/{slug}/git/trees/{urllib.parse.quote(branch)}", {"recursive": "1"})
    entries = (tree or {}).get("tree") if tree_code == 200 and isinstance(tree, dict) else []
    if isinstance(entries, list):
        for item in entries:
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            path = str(item.get("path") or "")
            if TEST_FILE_RE.search(path):
                test_files += 1
            if E2E_FILE_RE.search(path):
                e2e_files += 1

    coverage_pct = None
    coverage_url = ""
    sha_code, sha_payload = _get_allow(creds, f"/repos/{slug}/commits/{urllib.parse.quote(branch)}", {"per_page": 1})
    sha = ""
    if sha_code == 200 and isinstance(sha_payload, dict):
        sha = str(sha_payload.get("sha") or "")
    if sha:
        cr_code, cr_payload = _get_allow(creds, f"/repos/{slug}/commits/{sha}/check-runs", {"per_page": 30})
        runs = (cr_payload or {}).get("check_runs") if cr_code == 200 and isinstance(cr_payload, dict) else []
        if isinstance(runs, list):
            for run in runs:
                blob = " ".join(
                    [
                        str((run or {}).get("name") or ""),
                        str(((run or {}).get("output") or {}).get("title") or ""),
                        str(((run or {}).get("output") or {}).get("summary") or "")[:400],
                    ]
                )
                if "coverage" not in blob.lower() and "cobertura" not in blob.lower() and "lcov" not in blob.lower():
                    continue
                match = COV_PCT_RE.search(blob)
                if match:
                    try:
                        coverage_pct = float(match.group(1))
                        coverage_url = str((run or {}).get("html_url") or "")
                        break
                    except ValueError:
                        continue

    base = f"https://github.com/{slug}"
    search = f"https://github.com/search?q={urllib.parse.quote(f'repo:{slug}')}"
    prs = f"{base}/pulls?q="
    return {
        "merged_prs": merged if merged is not None else 0,
        "reverted_prs": reverted if reverted is not None else 0,
        "regression_prs": regression if regression is not None else 0,
        "test_files": test_files,
        "e2e_files": e2e_files,
        "coverage_pct": coverage_pct,
        "links": {
            "test_files": f"{search}+(path:test+OR+path:tests+OR+filename:test+OR+filename:spec)&type=code",
            "e2e_files": f"{search}+(e2e+OR+playwright+OR+cypress+OR+selenium)&type=code",
            "coverage": coverage_url or (f"{base}/commit/{sha}/checks" if sha else f"{base}/actions"),
            "merged_prs": prs + urllib.parse.quote(f"is:pr is:merged merged:>={since}"),
            "reverted_prs": prs + urllib.parse.quote(f"is:pr is:merged revert merged:>={since}"),
            "regression_prs": prs + urllib.parse.quote(f"is:pr is:merged regression merged:>={since}"),
        },
    }


E2E_RE = re.compile(
    r"e2e|end[-_ ]to[-_ ]end|playwright|cypress|selenium|test[-_ ]automation|"
    r"repodepot.*e2e|run e2e|ui[-_ ]test|integration[-_ ]test",
    re.I,
)


def _is_e2e_name(*parts: Any) -> bool:
    return bool(E2E_RE.search(" ".join(str(p or "") for p in parts)))


def _fetch_e2e(creds: dict[str, str], slug: str, days: int) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
    wf_code, wf_data = _get_allow(creds, f"/repos/{slug}/actions/workflows", {"per_page": 100})
    workflows = (wf_data or {}).get("workflows") if wf_code == 200 and isinstance(wf_data, dict) else []
    e2e_wfs = [
        w
        for w in (workflows or [])
        if isinstance(w, dict) and _is_e2e_name(w.get("name"), w.get("path"))
    ]
    runs: list[dict[str, Any]] = []
    targets = e2e_wfs[:6] or []
    for wf in targets:
        code, payload = _get_allow(
            creds,
            f"/repos/{slug}/actions/workflows/{wf.get('id')}/runs",
            {"per_page": 15},
        )
        found = (payload or {}).get("workflow_runs") if code == 200 and isinstance(payload, dict) else []
        if isinstance(found, list):
            runs.extend(r for r in found if isinstance(r, dict))
    if not targets:
        code, payload = _get_allow(creds, f"/repos/{slug}/actions/runs", {"per_page": 40})
        found = (payload or {}).get("workflow_runs") if code == 200 and isinstance(payload, dict) else []
        if isinstance(found, list):
            runs = [r for r in found if isinstance(r, dict) and _is_e2e_name(r.get("name"), r.get("path"), r.get("display_title"))]
    window_runs = []
    for run in runs:
        created = run.get("created_at") or run.get("updated_at")
        if not created:
            continue
        try:
            when = _parse_dt(created)
        except ValueError:
            continue
        if when >= cutoff:
            window_runs.append(run)
    window_runs.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "", reverse=True)
    completed = [r for r in window_runs if r.get("conclusion")]
    success = [r for r in completed if r.get("conclusion") == "success"]
    last = window_runs[0] if window_runs else (runs[0] if runs else None)
    return {
        "workflows": [
            {"id": w.get("id"), "name": w.get("name"), "path": w.get("path"), "state": w.get("state")}
            for w in e2e_wfs
        ],
        "workflow_count": len(e2e_wfs),
        "runs_in_window": len(window_runs),
        "success_pct": round(len(success) / len(completed) * 100) if completed else None,
        "last_conclusion": (last or {}).get("conclusion"),
        "last_status": (last or {}).get("status"),
        "last_name": (last or {}).get("name"),
        "last_url": (last or {}).get("html_url"),
        "last_at": (last or {}).get("updated_at") or (last or {}).get("created_at"),
        "actions_url": f"https://github.com/{slug}/actions",
        "runs_url": (last or {}).get("html_url")
        or (
            f"https://github.com/{slug}/actions/workflows/{str((e2e_wfs[0] or {}).get('path') or '').split('/')[-1]}"
            if e2e_wfs
            else f"https://github.com/{slug}/actions?query=e2e"
        ),
    }


def _score_e2e(metrics: dict[str, Any]) -> float:
    workflows = int(metrics.get("workflow_count") or 0)
    runs = int(metrics.get("runs_in_window") or 0)
    success = metrics.get("success_pct")
    last = metrics.get("last_conclusion")
    if not workflows and runs == 0:
        return 1.0
    if workflows and runs == 0:
        score = 2.2
    elif success is None:
        score = 2.5
    elif success >= 90:
        score = 4.6
    elif success >= 75:
        score = 4.0
    elif success >= 50:
        score = 3.0
    else:
        score = 1.8
    if last == "success":
        score = min(5.0, score + 0.3)
    elif last == "failure":
        score = max(0.5, score - 0.8)
    return round(score, 1)


def _why_e2e(metrics: dict[str, Any], score: float, rag: str, label: str) -> list[str]:
    why: list[str] = []
    if rag == "red":
        why.append(f"Red because GitHub E2E is {score}/5 (below 3.0) over {label}. Green needs ≥4.0.")
    elif rag == "amber":
        why.append(f"Amber because GitHub E2E is {score}/5 over {label}. Green needs ≥4.0.")
    else:
        why.append(f"Green: GitHub E2E is {score}/5 over {label}.")
    names = [str(w.get("name") or w.get("path")) for w in (metrics.get("workflows") or [])[:4]]
    if names:
        why.append("Workflows: " + ", ".join(names) + ".")
    else:
        why.append("No workflow name matching E2E / Playwright / Cypress / test automation.")
    runs = int(metrics.get("runs_in_window") or 0)
    if runs:
        pct = metrics.get("success_pct")
        why.append(f"{runs} E2E run(s) in {label}" + (f" · {pct}% succeeded." if pct is not None else "."))
    else:
        why.append(f"No E2E runs in {label}.")
    last = metrics.get("last_conclusion")
    if last:
        when = str(metrics.get("last_at") or "")[:10]
        why.append(f"Last run: {metrics.get('last_name') or 'E2E'} {last}" + (f" ({when})." if when else "."))
    return why[:6]


def _score(metrics: dict[str, Any]) -> float:
    score = 3.0
    protected = metrics.get("branch_protection")
    if protected is True:
        score += 0.8
    elif protected is False:
        score -= 1.5
    reviewed = float(metrics.get("reviewed_pct") or 0)
    if metrics.get("merged_prs_sampled"):
        if reviewed >= 85:
            score += 0.5
        elif reviewed < 70:
            score -= 0.8
    merge_h = float(metrics.get("median_merge_hours") or 0)
    if metrics.get("merged_prs_sampled"):
        if merge_h <= 16:
            score += 0.3
        elif merge_h > 48:
            score -= 0.5
    commits = float(metrics.get("commits_per_week") or 0)
    if commits >= 2:
        score += 0.4
    else:
        score -= 0.6
    freq = metrics.get("deployment_frequency")
    if freq == "high":
        score += 0.3
    elif freq == "medium":
        score += 0.1
    else:
        score -= 0.3
    return max(0.0, min(5.0, round(score, 1)))


def _why(metrics: dict[str, Any], score: float, rag: str, label: str) -> list[str]:
    why: list[str] = []
    if rag == "red":
        why.append(f"Red because live GitHub is {score}/5 (below 3.0) over {label}. Green needs ≥4.0.")
    elif rag == "amber":
        why.append(f"Amber because live GitHub is {score}/5 over {label}. Green needs ≥4.0.")
    else:
        why.append(f"Green: live GitHub is {score}/5 over {label}.")
    prot = metrics.get("branch_protection")
    branch = metrics.get("default_branch") or "main"
    if prot is True:
        why.append(f"{branch} is protected.")
    elif prot is False:
        why.append(f"{branch} has no branch protection (−1.5). Incoming inherits an unprotected default branch.")
    else:
        why.append(f"Could not read {branch} protection (need admin:repo or the branch is private to this token).")
    merged = int(metrics.get("merged_prs_sampled") or 0)
    if merged:
        why.append(
            f"{merged} merged PRs · median merge {metrics.get('median_merge_hours')}h · "
            f"{metrics.get('reviewed_pct')}% reviewed (sample {metrics.get('review_sample')})."
        )
    else:
        why.append(f"No merged PRs in {label}.")
    why.append(
        f"{metrics.get('commits_per_week')} commits/week · "
        f"{metrics.get('releases_per_week')} releases/week ({metrics.get('deployment_frequency')} deploy frequency)."
    )
    open_prs = int(metrics.get("open_prs") or 0)
    if open_prs:
        why.append(f"{open_prs} open PR(s).")
    merged = metrics.get("merged_prs")
    if merged is not None:
        why.append(
            f"{merged} merged PRs · {metrics.get('reverted_prs') or 0} reverts · "
            f"{metrics.get('regression_prs') or 0} regression PRs · "
            f"{metrics.get('test_files') or 0} test files · {metrics.get('e2e_files') or 0} E2E files"
            + (f" · coverage {metrics.get('coverage_pct')}%." if metrics.get("coverage_pct") is not None else ".")
        )
    return why[:7]


def assess_github(
    git: dict[str, str] | None = None,
    window_days: int = 90,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    days = int(window_days or 90)
    if days <= 0:
        days = 365
    days = min(days, 400)
    label = _window_label(days)
    slug = resolve_slug(git, catalog)
    url = f"https://github.com/{slug}" if slug else TOKEN_HELP
    creds = credentials()
    if not creds:
        return {
            "status": "disconnected",
            "rag": "amber",
            "title": "Not connected",
            "score": None,
            "summary": "Set GITHUB_TOKEN to pull live PR / protection / release metrics.",
            "repo": slug,
            "url": url,
            "window_days": days,
            "why": [
                "GitHub is not connected. Create a classic PAT at https://github.com/settings/tokens (repo or public_repo).",
                "Put GITHUB_TOKEN=... in handover-dashboard/.env.",
            ],
            "setup": [
                f"Create a token at {TOKEN_HELP} with repo (private) or public_repo.",
                "Put GITHUB_TOKEN=... in handover-dashboard/.env.",
            ],
            "signals": {},
            "gap": {
                "id": "github.disconnected",
                "area": "release",
                "title": "GitHub not connected — release score is git-only",
                "ask": "Connect GitHub so handover scoring includes branch protection, review coverage, and deploy frequency.",
                "p0": False,
            },
        }
    if not slug:
        return {
            "status": "skipped",
            "rag": "amber",
            "title": "No GitHub remote",
            "score": None,
            "summary": "No GitHub repo on origin, catalog github.com/project-slug, or the known Task Mining map.",
            "repo": "",
            "url": "",
            "window_days": days,
            "why": [
                "Need a github.com remote, catalog annotation github.com/project-slug, or a known service id (task-mining, task-mining-ai, …)."
            ],
            "signals": {},
            "gap": None,
        }

    try:
        metrics = _fetch_repo(creds, slug, days)
        try:
            e2e_metrics = _fetch_e2e(creds, slug, days)
        except Exception as exc:
            e2e_metrics = {"error": str(exc)[:240]}
    except Exception as exc:
        return {
            "status": "error",
            "rag": "red",
            "title": "GitHub call failed",
            "score": None,
            "summary": str(exc)[:300],
            "repo": slug,
            "url": url,
            "window_days": days,
            "why": ["Could not read the GitHub repo. Check GITHUB_TOKEN scopes and that the token can see this repository."],
            "errors": [str(exc)[:300]],
            "signals": {},
            "gap": {
                "id": "github.error",
                "area": "release",
                "title": "GitHub API failed",
                "ask": "Fix GITHUB_TOKEN (repo/public_repo) for this origin remote.",
                "p0": False,
            },
        }

    if e2e_metrics.get("error"):
        e2e_live = {
            "status": "error",
            "rag": "amber",
            "title": "E2E fetch failed",
            "score": None,
            "summary": e2e_metrics["error"],
            "url": f"https://github.com/{slug}/actions",
            "window_days": days,
            "why": [e2e_metrics["error"]],
            "signals": {},
            "gap": None,
        }
    else:
        e2e_score = _score_e2e(e2e_metrics)
        e2e_rag = rag_from_score(e2e_score)
        e2e_gap = None
        if not e2e_metrics.get("workflow_count") and not e2e_metrics.get("runs_in_window"):
            e2e_gap = {
                "id": "github.e2e.missing",
                "area": "testing",
                "title": f"No E2E / test automation workflow on {slug}",
                "ask": "Add a GitHub Actions E2E (or Playwright/Cypress) workflow so incoming can see how production paths are verified.",
                "p0": False,
            }
        elif e2e_score < 3.0:
            e2e_gap = {
                "id": "github.e2e.weak",
                "area": "testing",
                "title": f"GitHub E2E is {e2e_score}/5 over {label}",
                "ask": "Get the E2E suite running green in the selected window before treating automation as inherited.",
                "p0": e2e_score < 2.0,
            }
        e2e_live = {
            "status": "connected",
            "rag": e2e_rag,
            "title": "GitHub Actions E2E",
            "score": e2e_score,
            "summary": (
                f"{e2e_metrics.get('workflow_count') or 0} E2E workflow(s), "
                f"{e2e_metrics.get('runs_in_window') or 0} run(s) in {label}"
                + (
                    f", {e2e_metrics.get('success_pct')}% succeeded"
                    if e2e_metrics.get("success_pct") is not None
                    else ""
                )
                + f". Live E2E {e2e_score}/5."
            ),
            "url": e2e_metrics.get("last_url") or e2e_metrics.get("actions_url") or f"https://github.com/{slug}/actions",
            "window_days": days,
            "window_label": label,
            "why": _why_e2e(e2e_metrics, e2e_score, e2e_rag, label),
            "signals": e2e_metrics,
            "gap": e2e_gap,
        }

    score = _score(metrics)
    rag = rag_from_score(score)
    why = _why(metrics, score, rag, label)
    gap = None
    if metrics.get("branch_protection") is False:
        gap = {
            "id": "github.unprotected",
            "area": "release",
            "title": f"{metrics.get('default_branch')} is not protected on {slug}",
            "ask": "Turn on branch protection (reviews + no force-push) before the new team inherits the default branch.",
            "p0": True,
        }
    elif score < 3.0:
        gap = {
            "id": "github.weak",
            "area": "release",
            "title": f"GitHub delivery is {score}/5 over {label}",
            "ask": "Raise review coverage, merge time, or deploy cadence before treating the pipeline as handover-ready.",
            "p0": score < 2.0,
        }

    return {
        "status": "connected",
        "rag": rag,
        "title": "Connected",
        "score": score,
        "summary": (
            f"{slug} over {label}: {metrics.get('merged_prs_sampled')} merged PRs, "
            f"{metrics.get('reviewed_pct')}% reviewed, "
            f"{metrics.get('commits_per_week')} commits/week. Live GitHub {score}/5."
        ),
        "repo": slug,
        "url": metrics.get("url") or url,
        "window_days": days,
        "window_label": label,
        "why": why,
        "signals": metrics,
        "gap": gap,
        "e2e": e2e_live,
    }


def apply_github(report: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    extra_reds = list(report.get("p0_reds") or [])
    ids = {g.get("id") for g in report.get("gaps") or []}

    def _add_gap(gap: dict[str, Any] | None) -> None:
        if not gap:
            return
        if gap.get("id") not in ids:
            report.setdefault("gaps", []).append(gap)
            ids.add(gap.get("id"))
        if gap.get("p0") and gap.get("id") and gap["id"] not in extra_reds:
            extra_reds.append(gap["id"])

    _add_gap(live.get("gap"))
    e2e = live.get("e2e") or {}
    _add_gap(e2e.get("gap"))
    blended = False
    if live.get("status") == "connected" and live.get("score") is not None:
        for area in report.get("areas") or []:
            if area.get("id") != "release":
                continue
            git_score = float(area.get("score") or 0)
            mixed = round(git_score * 0.55 + float(live["score"]) * 0.45, 1)
            area["git_score"] = git_score
            area["github_score"] = live["score"]
            area["score"] = mixed
            area["score_pct"] = int(round((mixed / 5) * 100))
            area["rag"] = rag_from_score(mixed)
            area["why"] = f"Git {git_score}/5 blended with GitHub {live['score']}/5."
            blended = True
    if e2e.get("score") is not None:
        found = False
        for area in report.get("areas") or []:
            if area.get("id") != "testing":
                continue
            git_score = float(area.get("score") or 0)
            mixed = round(git_score * 0.4 + float(e2e["score"]) * 0.6, 1)
            area["git_score"] = git_score
            area["e2e_score"] = e2e["score"]
            area["score"] = mixed
            area["score_pct"] = int(round((mixed / 5) * 100))
            area["rag"] = rag_from_score(mixed)
            area["why"] = f"Git {git_score}/5 blended with GitHub E2E {e2e['score']}/5."
            found = True
            blended = True
        if not found:
            report.setdefault("areas", []).append(
                {
                    "id": "testing",
                    "label": "E2E / test automation",
                    "weight": 0.08,
                    "p0": True,
                    "score": e2e["score"],
                    "score_pct": int(round((float(e2e["score"]) / 5) * 100)),
                    "rag": e2e.get("rag") or rag_from_score(e2e["score"]),
                    "e2e_score": e2e["score"],
                    "why": e2e.get("summary") or f"Live GitHub E2E {e2e['score']}/5.",
                    "checks": [],
                }
            )
            blended = True
    if blended or extra_reds:
        from scanner.score import recompute_overall

        recompute_overall(report, extra_p0_reds=extra_reds)
    report["github"] = live
    return report
