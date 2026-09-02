from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from scanner.env import load_dotenv_files
from scanner.files import read_text
from scanner.roadie import APP_BASE, credentials, _get, _try_get

DEFAULT_GROUP = "group:default/task-mining"
SERVICE_TYPES = {"service"}
DIR_ALIASES = {
    "task-mining": "cloud-task-mining",
}


def parse_group_ref(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return DEFAULT_GROUP
    match = re.search(r"/catalog/([^/]+)/group/([^/?#]+)", text)
    if match:
        return f"group:{match.group(1)}/{match.group(2)}"
    if text.startswith("group:"):
        return text
    if "/" not in text:
        return f"group:default/{text}"
    if text.count("/") == 1 and ":" not in text:
        return f"group:{text}"
    return text


def is_group_query(value: str) -> bool:
    text = (value or "").strip()
    lowered = text.lower()
    if not lowered:
        return False
    if lowered.startswith(("http://", "https://", "https:/", "http:/")):
        return "/group/" in lowered or "roadie.so" in lowered
    if "/catalog/" in lowered and "/group/" in lowered:
        return True
    if lowered.startswith("group:"):
        return True
    return False


def group_url(ref: str) -> str:
    kind, _, rest = ref.partition(":")
    namespace, _, name = rest.partition("/")
    if not name:
        namespace, name = "default", rest
    return f"{APP_BASE}/catalog/{namespace}/{kind}/{name}"


def _entity_to_service(row: dict[str, Any]) -> dict[str, Any] | None:
    if (row.get("kind") or "").lower() != "component":
        return None
    spec = row.get("spec") or {}
    if str(spec.get("type") or "").lower() not in SERVICE_TYPES:
        return None
    meta = row.get("metadata") or {}
    name = str(meta.get("name") or "").strip()
    if not name:
        return None
    anns = meta.get("annotations") or {}
    slug = str(anns.get("github.com/project-slug") or "").strip()
    remote = f"https://github.com/{slug}.git" if slug else ""
    return {
        "id": name,
        "ref": f"component:default/{name}",
        "title": meta.get("title") or name,
        "type": spec.get("type"),
        "owner": spec.get("owner"),
        "lifecycle": spec.get("lifecycle"),
        "tier": spec.get("tier"),
        "description": meta.get("description") or "",
        "url": f"{APP_BASE}/catalog/default/component/{name}",
        "github_slug": slug,
        "remote": remote,
        "catalog": {
            "name": name,
            "title": meta.get("title") or name,
            "description": meta.get("description"),
            "owner": spec.get("owner"),
            "lifecycle": spec.get("lifecycle"),
            "tier": spec.get("tier"),
            "type": spec.get("type"),
            "annotations": anns,
        },
    }


def fetch_group_services(group_ref: str | None = None) -> dict[str, Any]:
    load_dotenv_files()
    ref = parse_group_ref(group_ref or DEFAULT_GROUP)
    _, _, rest = ref.partition(":")
    namespace, _, name = rest.partition("/")
    if not name:
        namespace, name = "default", rest
    creds = credentials()
    services: list[dict[str, Any]] = []
    title = name.replace("-", " ").title()
    source = "fallback"
    if creds:
        listing = {**creds, "timeout": min(float(creds.get("timeout") or 45), 6)}
        with ThreadPoolExecutor(max_workers=2) as pool:
            entity_f = pool.submit(
                _try_get,
                listing,
                f"{creds['api_base']}/catalog/entities/by-name/group/{namespace}/{name}",
            )
            owned_f = pool.submit(
                _try_get,
                listing,
                f"{creds['api_base']}/catalog/entities?filter=kind=component,relations.ownedBy={ref}",
            )
            entity = entity_f.result()
            owned = owned_f.result()
        if isinstance(entity, dict):
            title = ((entity.get("metadata") or {}).get("title") or title)
        rows = owned if isinstance(owned, list) else []
        for row in rows:
            if isinstance(row, dict):
                item = _entity_to_service(row)
                if item:
                    services.append(item)
        if services:
            source = "roadie"
    if not services:
        from scanner.github import KNOWN_SLUGS

        def _fallback(sid: str, title: str) -> dict[str, Any]:
            slug = KNOWN_SLUGS.get(sid) or ""
            return {
                "id": sid,
                "ref": f"component:default/{sid}",
                "title": title,
                "type": "service",
                "url": f"{APP_BASE}/catalog/default/component/{sid}",
                "github_slug": slug,
                "remote": f"https://github.com/{slug}.git" if slug else "",
                "catalog": {
                    "name": sid,
                    "title": title,
                    "type": "service",
                    "annotations": {"github.com/project-slug": slug} if slug else {},
                },
            }

        services = [
            _fallback("task-mining", "Cloud Task Mining"),
            _fallback("task-mining-ai", "Task Mining AI"),
            _fallback("task-mining-uploader", "Task Mining Uploader"),
            _fallback("tm-image-collector", "Tm Image Collector"),
        ]
    seen: set[str] = set()
    unique = []
    for item in services:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        unique.append(item)
    return {
        "id": name,
        "ref": ref,
        "title": title,
        "url": group_url(ref),
        "source": source,
        "services": unique,
    }


def _catalog_name(root: Path) -> str:
    for fname in ("catalog-info.yaml", "catalog-info.yml"):
        path = root / fname
        if not path.is_file():
            continue
        try:
            import yaml

            docs = list(yaml.safe_load_all(read_text(path)))
        except Exception:
            continue
        for doc in docs:
            if isinstance(doc, dict) and doc.get("kind") == "Component":
                return str((doc.get("metadata") or {}).get("name") or "")
    return ""


def workspace_roots(hint: str | None = None) -> list[Path]:
    load_dotenv_files()
    out: list[Path] = []
    for raw in (
        hint,
        os.environ.get("HANDOVER_WORKSPACE"),
        "/Users/m.kumar/celonis/task-mining",
        str(Path.home() / "celonis" / "task-mining"),
    ):
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path.is_file():
            path = path.parent
        if path.is_dir() and path not in out:
            out.append(path)
            if path.name != "task-mining" and (path / "task-mining").is_dir():
                out.append(path / "task-mining")
            if path.parent.name == "task-mining" or path.name in DIR_ALIASES.values():
                parent = path.parent
                if parent.is_dir() and parent not in out:
                    out.append(parent)
    return out


def resolve_service_path(service_id: str, workspaces: list[Path]) -> str:
    aliases = {service_id, DIR_ALIASES.get(service_id, ""), service_id.replace("_", "-")}
    aliases.discard("")
    for root in workspaces:
        for alias in aliases:
            candidate = root / alias
            if candidate.is_dir() and (candidate / ".git").exists():
                return str(candidate.resolve())
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            name = _catalog_name(child)
            if name == service_id:
                return str(child.resolve())
    return ""


def attach_local_paths(group: dict[str, Any], workspace: str | None = None) -> dict[str, Any]:
    roots = workspace_roots(workspace)
    for svc in group.get("services") or []:
        svc["path"] = resolve_service_path(svc["id"], roots)
    return group
