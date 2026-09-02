from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv_files() -> None:
    """Load handover-dashboard/.env, then sibling onboarding-readiness for live-source keys."""
    local = ROOT / ".env"
    sibling = ROOT.parent / "onboarding-readiness" / ".env"
    for env in (local, sibling):
        if not env.exists():
            continue
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key or not value:
                continue
            if env != local and not key.startswith(
                ("DD_", "ROADIE_", "DATADOG_", "CELONIS_", "GITHUB_", "GH_", "JIRA_", "CONFLUENCE_")
            ):
                continue
            # Local .env wins so rotated Datadog tokens take effect without a restart.
            if env == local:
                os.environ[key] = value
            else:
                os.environ.setdefault(key, value)
