from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from scanner import is_group_query, scan_group, scan_repo, scan_service, to_markdown
from scanner.criteria import criteria_catalog
from scanner.groups import attach_local_paths, fetch_group_services

ROOT = Path(__file__).parent
STATIC = ROOT / "static"

app = FastAPI(title="Handover readiness scanner", version="1.0.0")
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


class Signoff(BaseModel):
    kt_recorded: bool = False
    dashboards_verified: bool = False
    access_transferred: bool = False
    primary_shadowing: bool = False
    reverse_incidents: int = Field(0, ge=0, le=2)


class PillarWeights(BaseModel):
    shadow: int = Field(30, ge=0, le=80)
    docs: int = Field(25, ge=0, le=80)
    observe: int = Field(25, ge=0, le=80)
    debt: int = Field(20, ge=0, le=80)


class ScanRequest(BaseModel):
    path: str = Field(..., min_length=1, description="Repo path or Roadie group URL")
    signoff: Optional[Signoff] = None
    window_days: int = Field(90, ge=1, le=400, description="Live-source lookback: 7/14/30/90/180/365")
    weights: Optional[PillarWeights] = None
    workspace: Optional[str] = None
    services: Optional[dict[str, "ServiceTune"]] = None
    selected: Optional[list[str]] = None


class ServiceTune(BaseModel):
    path: Optional[str] = None
    signoff: Optional[Signoff] = None
    weights: Optional[PillarWeights] = None


class ServiceScanRequest(BaseModel):
    service_id: str = Field(..., min_length=1)
    path: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    service_type: Optional[str] = None
    github_slug: Optional[str] = None
    window_days: int = Field(90, ge=1, le=400)
    workspace: Optional[str] = None
    signoff: Optional[Signoff] = None
    weights: Optional[PillarWeights] = None


ScanRequest.model_rebuild()


def _tunes(raw: dict[str, ServiceTune] | None) -> dict[str, dict]:
    if not raw:
        return {}
    out = {}
    for key, tune in raw.items():
        out[key] = {
            "path": tune.path,
            "signoff": tune.signoff.model_dump() if tune.signoff else None,
            "weights": tune.weights.model_dump() if tune.weights else None,
        }
    return out


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/group")
def api_group(ref: str = Query("group:default/task-mining"), workspace: Optional[str] = None) -> dict:
    try:
        return attach_local_paths(fetch_group_services(ref), workspace)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/scan/service")
async def api_scan_service(body: ServiceScanRequest) -> dict:
    try:
        report = await run_in_threadpool(
            scan_service,
            body.service_id,
            path=body.path,
            title=body.title,
            url=body.url,
            service_type=body.service_type,
            github_slug=body.github_slug,
            window_days=body.window_days,
            workspace=body.workspace,
            signoff=body.signoff.model_dump() if body.signoff else None,
            weights=body.weights.model_dump() if body.weights else None,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    try:
        report["markdown"] = to_markdown(report)
    except Exception:
        report["markdown"] = ""
    return report


@app.post("/api/scan")
def api_scan(body: ScanRequest) -> dict:
    raw = body.path.strip()
    if is_group_query(raw) or raw.lower() in {"task-mining", "group:default/task-mining"}:
        try:
            report = scan_group(
                raw,
                window_days=body.window_days,
                workspace=body.workspace or raw if raw.startswith("/") else body.workspace,
                services=_tunes(body.services),
                selected=body.selected,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        report["markdown"] = to_group_markdown(report)
        return report
    if raw.startswith("~/"):
        raw = str(Path.home() / raw[2:])
    path = Path(raw).expanduser()
    try:
        path = path.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Path does not exist: {path}")
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="Path must be a directory")
    if path == Path("/"):
        raise HTTPException(status_code=400, detail="Refusing to scan filesystem root")
    try:
        report = scan_repo(
            path,
            signoff=body.signoff.model_dump() if body.signoff else None,
            window_days=body.window_days,
            weights=body.weights.model_dump() if body.weights else None,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    report["markdown"] = to_markdown(report)
    return report


def to_group_markdown(report: dict) -> str:
    g = report.get("group") or {}
    v = report.get("verdict") or {}
    lines = [
        f"# Handover readiness: {g.get('title') or 'Group'}",
        "",
        f"**Group:** [{g.get('ref')}]({g.get('url')})  ",
        f"**Services:** {g.get('service_count')}  ",
        f"**Verdict:** {v.get('label')} · **{report.get('overall_pct')}/100** · {str(report.get('overall_rag') or '').upper()}",
        "",
        v.get("detail") or "",
        "",
    ]
    for svc in report.get("services") or []:
        ho = svc.get("handover") or {}
        vv = svc.get("verdict") or {}
        lines += [
            f"## {svc.get('service_title') or svc.get('service_id')}",
            "",
            f"- Score: {ho.get('points', svc.get('overall_pct'))}/{ho.get('max', 100)} · {vv.get('label')}",
        ]
        if svc.get("missing_clone"):
            lines.append("- No local clone — live Roadie/Datadog/GitHub only.")
        if svc.get("error"):
            lines.append(f"- Error: {svc['error']}")
        lines.append("")
    return "\n".join(lines)


@app.get("/api/criteria")
def api_criteria() -> dict:
    return criteria_catalog()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "3847"))
    uvicorn.run("app:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    main()
