from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scanner.env import load_dotenv_files
from scanner.score import rag_from_score, recompute_overall

DEFAULT_FOLDER_ID = "1fB6CtN8Gh2L0RtwBXv31Iy546H5BpkD4"
FOLDER_URL = "https://drive.google.com/drive/folders/{id}"

TM_HINT = re.compile(r"task[-_ ]?mining|taskmining|tm-image|tmctmg", re.I)
HANDOVER_HINT = re.compile(
    r"handover|onboarding|knowledge transfer|\bkt\b|walkthrough|architecture overview|on-call",
    re.I,
)
VIDEO_HINT = re.compile(r"recording|\.mp4$|\.mkv$|video/", re.I)


def is_task_mining(catalog: dict[str, Any] | None, root_name: str = "") -> bool:
    blob = " ".join(
        str(x or "")
        for x in (
            root_name,
            (catalog or {}).get("name"),
            (catalog or {}).get("title"),
            (catalog or {}).get("description"),
        )
    )
    return bool(TM_HINT.search(blob))


def _folder_id() -> str:
    load_dotenv_files()
    return (os.environ.get("GOOGLE_DRIVE_FOLDER_ID") or DEFAULT_FOLDER_ID).strip()


def _drivefs_db() -> Path | None:
    root = Path.home() / "Library/Application Support/Google/DriveFS"
    if not root.is_dir():
        return None
    dbs = sorted(root.glob("*/metadata_sqlite_db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return dbs[0] if dbs else None


def _when(ts: int | None) -> datetime | None:
    if not ts:
        return None
    try:
        if ts > 10**14:
            return datetime.fromtimestamp(ts / 1e6, tz=timezone.utc)
        if ts > 10**11:
            return datetime.fromtimestamp(ts / 1e3, tz=timezone.utc)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _children(cur: sqlite3.Cursor, parent_sid: int) -> list[sqlite3.Row]:
    return cur.execute(
        """
        SELECT i.stable_id, i.id, i.local_title, i.mime_type, i.is_folder, i.file_size, i.modified_date
        FROM stable_parents p
        JOIN items i ON i.stable_id = p.item_stable_id
        WHERE p.parent_stable_id = ? AND i.trashed = 0 AND i.is_tombstone = 0
        """,
        (parent_sid,),
    ).fetchall()


def _walk(cur: sqlite3.Cursor, parent_sid: int, prefix: str = "", depth: int = 0, max_depth: int = 6) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in _children(cur, parent_sid):
        title = row["local_title"] or ""
        path = f"{prefix}/{title}" if prefix else title
        rec = {
            "title": title,
            "path": path,
            "mime": row["mime_type"] or "",
            "folder": bool(row["is_folder"]),
            "size": int(row["file_size"] or 0),
            "modified": _when(row["modified_date"]),
        }
        out.append(rec)
        if rec["folder"] and depth < max_depth:
            out.extend(_walk(cur, row["stable_id"], path, depth + 1, max_depth))
    return out


def _disconnected(reason: str, setup: list[str]) -> dict[str, Any]:
    return {
        "status": "disconnected",
        "title": "Drive not connected",
        "summary": reason,
        "score": None,
        "rag": "amber",
        "url": FOLDER_URL.format(id=_folder_id()),
        "findings": [reason],
        "setup": setup,
        "kt_ready": False,
        "kt_recordings": 0,
    }


def assess_drive(catalog: dict[str, Any] | None = None, root_name: str = "") -> dict[str, Any]:
    folder_id = _folder_id()
    url = FOLDER_URL.format(id=folder_id)
    if not is_task_mining(catalog, root_name):
        return {
            "status": "skipped",
            "title": "Drive skipped",
            "summary": "Google Drive pack is scored for Task Mining repos only.",
            "score": None,
            "rag": "amber",
            "url": url,
            "findings": [],
            "kt_ready": False,
            "kt_recordings": 0,
        }
    db = _drivefs_db()
    if not db:
        return _disconnected(
            "Google Drive for Desktop metadata not found on this Mac.",
            [
                "Sign in to Google Drive for Desktop with your Celonis account.",
                f"Open {url} once so Drive syncs the folder.",
            ],
        )
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        sid_row = cur.execute("SELECT stable_id FROM stable_ids WHERE cloud_id=?", (folder_id,)).fetchone()
        if not sid_row:
            sid_row = cur.execute("SELECT stable_id FROM items WHERE id=?", (folder_id,)).fetchone()
        if not sid_row:
            return _disconnected(
                "Folder is not in the local Drive cache. Open it once in Drive for Desktop.",
                [f"Open {url} while signed in as your Celonis user, then rescan."],
            )
        folder = cur.execute(
            "SELECT local_title FROM items WHERE stable_id=?", (sid_row["stable_id"],)
        ).fetchone()
        title = (folder["local_title"] if folder else None) or "Task Mining Drive"
        items = _walk(cur, sid_row["stable_id"])
        con.close()
    except sqlite3.Error as exc:
        return _disconnected(f"Could not read Drive metadata ({exc}).", [f"Open {url} and rescan."])

    return _score(title, url, items)


def _score(title: str, url: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    files = [x for x in items if not x["folder"]]
    docs = [
        x
        for x in files
        if any(
            k in (x["mime"] or "")
            for k in ("google-apps.document", "google-apps.presentation", "google-apps.spreadsheet", "pdf", "officedocument")
        )
    ]
    recordings = [
        x
        for x in files
        if (x["mime"] or "").startswith("video/") or bool(VIDEO_HINT.search(x["title"] or "") and x["size"] > 1_000_000)
    ]
    kt_recordings = [
        x
        for x in recordings
        if x["size"] >= 20_000_000
        and (
            HANDOVER_HINT.search(x["path"] or "")
            or "Handover Material" in (x["path"] or "")
            or "Onboarding_Feb2026" in (x["path"] or "")
        )
    ]
    handover_docs = [
        x
        for x in docs
        if HANDOVER_HINT.search(x["title"] or "") or HANDOVER_HINT.search(x["path"] or "")
    ]
    substantial_docs = [x for x in handover_docs if x["size"] >= 20_000]
    empty_named = [x for x in docs if x["size"] == 0 and ("11 - Documentation" in (x["path"] or "") or "09 - Onboarding" in (x["path"] or ""))]

    rec_score = 0.0
    if len(kt_recordings) >= 8:
        rec_score = 5.0
    elif len(kt_recordings) >= 3:
        rec_score = 4.0
    elif len(kt_recordings) >= 1:
        rec_score = 3.0
    elif recordings:
        rec_score = 1.5

    doc_score = 1.5
    if any("Engineering Handover" in (x["title"] or "") and x["size"] >= 100_000 for x in docs):
        doc_score += 1.2
    if any("Components Handover" in (x["title"] or "") and x["size"] >= 100_000 for x in docs):
        doc_score += 0.8
    if len(substantial_docs) >= 5:
        doc_score += 0.6
    if empty_named:
        doc_score -= 0.8
    doc_score = max(0.0, min(5.0, round(doc_score, 1)))

    quality = round(rec_score * 0.55 + doc_score * 0.45, 1)
    rag = rag_from_score(quality)
    kt_ready = len(kt_recordings) >= 3

    findings: list[str] = []
    if kt_recordings:
        findings.append(
            f"{len(kt_recordings)} substantial handover recordings (architecture, on-call, gateway, frontend, service)."
        )
    else:
        findings.append("No substantial handover/onboarding recordings found in this Drive folder.")
    if any("Engineering Handover" in (x["title"] or "") for x in docs):
        findings.append("Task Mining Engineering Handover doc is present and sizable.")
    if empty_named:
        findings.append(
            f"{len(empty_named)} docs in Onboarding/Documentation folders are empty stubs (size 0) — file count is not quality."
        )
    findings.append("Incoming still needs to watch the KT pack; recordings existing is not reverse shadowing.")

    names = [x["title"] for x in sorted(kt_recordings, key=lambda r: -r["size"])[:6]]
    summary = f"{len(kt_recordings)} KT recordings · {len(substantial_docs)} handover docs · {len(empty_named)} empty stubs"

    gap = None
    if not kt_ready:
        gap = {
            "id": "drive.kt",
            "area": "knowledge",
            "title": "Drive has no usable KT recording pack",
            "ask": "Record architecture, on-call, deploy, and component walkthroughs and store them in the squad Drive.",
            "p0": True,
        }
    elif empty_named and rag != "green":
        gap = {
            "id": "drive.docs",
            "area": "knowledge",
            "title": "Drive documentation folder has empty stubs",
            "ask": "Replace empty Onboarding/Documentation gdocs or archive them so the incoming team does not treat them as SOPs.",
            "p0": False,
        }

    return {
        "status": "connected",
        "title": title,
        "summary": summary,
        "score": quality,
        "rag": rag,
        "url": url,
        "findings": findings[:5],
        "kt_ready": kt_ready,
        "kt_recordings": len(kt_recordings),
        "counts": {
            "files": len(files),
            "docs": len(docs),
            "recordings": len(recordings),
            "kt_recordings": len(kt_recordings),
            "handover_docs": len(handover_docs),
            "empty_named": len(empty_named),
        },
        "examples": names,
        "rec_score": rec_score,
        "doc_score": doc_score,
        "gap": gap,
    }


def apply_drive(report: dict[str, Any], assessed: dict[str, Any]) -> dict[str, Any]:
    if assessed.get("status") != "connected" or assessed.get("score") is None:
        limits = list(report.get("limits") or [])
        if assessed.get("status") == "disconnected":
            limits.insert(0, "Google Drive is not readable here; Documentation/KT is git-only until Drive for Desktop syncs the folder.")
        report["limits"] = limits
        report["drive"] = assessed
        return report

    quality = float(assessed["score"])
    for area in report.get("areas") or []:
        if area.get("id") != "knowledge":
            continue
        prior = float(area.get("score") or 0)
        mixed = round(prior * 0.55 + quality * 0.45, 1)
        area["git_score"] = area.get("git_score", prior)
        area["drive_score"] = quality
        area["score"] = mixed
        area["score_pct"] = int(round((mixed / 5) * 100))
        area["rag"] = rag_from_score(mixed)
        area["why"] = f"Git {prior}/5 blended with Drive {quality}/5 · {assessed.get('summary') or ''}".strip()
        checks = list(area.get("checks") or [])
        status = "found" if assessed.get("kt_ready") else "missing"
        checks.append(
            {
                "id": "knowledge.drive_kt",
                "area": "knowledge",
                "weight": 0,
                "status": status,
                "score": 1.0 if assessed.get("kt_ready") else 0.0,
                "evidence": "Drive KT recordings (handover pack)",
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
    limits = [x for x in (report.get("limits") or []) if "Google Drive" not in x]
    limits.insert(
        0,
        "Knowledge is 55% git + 45% Google Drive (KT recordings vs empty/stale docs in the squad shared folder). "
        "Recordings existing is not the same as incoming having watched them.",
    )
    report["limits"] = limits
    report["drive"] = assessed
    return report
