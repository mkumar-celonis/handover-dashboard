# Handover readiness dashboard

Local scanner that scores **any git repo** for team handover — the same 8-area scorecard used for Cloud Task Mining.

It reads the filesystem only (catalog, runbooks, CI, ADRs, CODEOWNERS, …). It does **not** upload the repo.

## Run

```bash
cd /Users/m.kumar/celonis/stretch-goals/handover-dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open [http://127.0.0.1:3847](http://127.0.0.1:3847) and paste a repo path, for example:

- `/Users/m.kumar/celonis/task-mining/cloud-task-mining`
- `/Users/m.kumar/celonis/task-mining/task-mining-gateway`

CLI (markdown or JSON, no browser):

```bash
python -m scanner /path/to/repo
python -m scanner /path/to/repo --json
```

## Criteria

Open **Criteria & scoring** on the dashboard (or `GET /api/criteria`) for the finalised list: exit decision cuts, pillar formulas, every git file check, and how Datadog / GitHub / E2E / Jira / Roadie / Drive / runbooks / Kargo / Code Purple become green, amber, or red.

## Scorecard

Exit score out of **100**, **weighted by risk, not evenly**. Reverse shadowing carries the most. Git scan fills three pillars; reverse shadowing is a people sign-off.

| Pillar | Max | Pass |
|---|---|---|
| Reverse Shadowing | 30 | 24 |
| Documentation & SOPs | 25 | 20 |
| Observability & Access | 25 | 20 |
| Backlog & Tech Debt | 20 | 15 |

These defaults live in `scanner/checks.yaml` (`pillars:`). They are **service/project-specific** — retune with the **sliders on each service card** (saved per service, total stays 100), or put a `handover.yaml` in the repo root.

## Groups (Roadie)

Paste a Roadie group URL such as [task-mining](https://celonis.roadie.so/catalog/default/group/task-mining) and click **Load services**. The dashboard lists every **service** in the group (today: Cloud Task Mining, Task Mining AI, Task Mining Uploader, Tm Image Collector) with select/unselect and **Score weights** per service. Scorecards are fetched only after **Scan selected services**. Group headline is the average; verdict is the weakest selected service.

Local clones are resolved under `HANDOVER_WORKSPACE` (default `/Users/m.kumar/celonis/task-mining`). A service without a clone still gets live Roadie / Datadog / GitHub if annotated.

```yaml
pillars:
  shadow: 30
  docs: 25
  observe: 25
  debt: 20
```

Pass is 80% of each pillar. Decision thresholds stay 85 / 70 of the total.

**Decision:** ≥85 and all pillars pass → complete handover. 70–84 → soft handover (outgoing stays 2 weeks). &lt;70 or no shadowing → block / escalate.

## Platform programs

Scanned separately and can block handover even when the 8-area score is high.

| Program | Adopted | Partial | Missing | N/A |
|---|---|---|---|---|
| **Kargo** | `repo-depot.celonis.dev/deploy-strategy: kargo` | RepoDepot/Kargo traces without the annotation | Cloud `service` with neither | On-prem / library |
| **Code Purple** | `*-purple` verification lane **and** (quality-prioritization workflow or STEP/deployment-verification) | Some of: purple tenant, `code-quality-prioritization` workflow, Sonar, STEP | None of those | — |

Kargo not adopted on a production cloud service is **P0**. Code Purple incomplete is **P0 on tier-1**.

## GitHub (live delivery)

Pulls origin `github.com` repo metrics for the selected **time window** (1 week … 12 months): open/merged PRs, median merge hours, review coverage, default-branch protection, commits/week, releases/week.

Set `GITHUB_TOKEN` in `.env` — classic PAT from [github.com/settings/tokens](https://github.com/settings/tokens) with `repo` (private) or `public_repo`.

**Release** is then `55% git + 45% GitHub`. Missing branch protection is P0. Green is ≥4.0.

The same window is applied to Datadog incidents (monitors/SLOs stay current-state). Default is **90 days**. CLI: `python -m scanner /path --window-days 30`. Query: `/?path=...&w=30`.

## Code Purple metrics (live Celocore)

Pulls the [Code Purple metrics](https://celocore.us-2.celonis.cloud/package-manager/ui/views/ui/spaces/f88d799c-2272-4a3c-8335-29c05d37acdf/packages/cd114198-b0ef-431b-ad0b-b19455502e5f/nodes/ad1b7411-7f8a-47c8-980e-2dae22373407?activeTabs=code-purple-metrics:5655bef6-d3fe-4e48-85bb-cdddaffa09ce) Studio view on **celocore.us-2**.

The view is not public. Set `CELONIS_API_TOKEN` (User API key, `Authorization: Bearer`) or `CELONIS_APP_KEY` (Application key) in `.env`, and grant **USE PACKAGE** on that Studio package. Create keys from [Edit Profile](https://celocore.us-2.celonis.cloud/ui/team#/profile) or [Applications](https://celocore.us-2.celonis.cloud/ui/team/settings/applications).

Live metrics blend as **55% git Code Purple + 45% Celocore KPIs** when a numeric row matches the scanned service (task-mining). Green is ≥4.0 (~80% coverage/quality).

## Datadog (live operate)

Prefers a **Personal or Service Access Token** as `Authorization: Bearer` ([Datadog Access Tokens](https://docs.datadoghq.com/account_management/personal-access-tokens/)). Create one at [Personal Settings → Access Tokens](https://celonis.datadoghq.com/personal-settings/access-tokens). The secret starts with `ddpat_` or `ddsat_` and is shown only once.

Set `DD_ACCESS_TOKEN` in `handover-dashboard/.env`. Needed scopes: `monitors_read`, `slos_read`, `incident_read`. `DD_SITE=https://api.datadoghq.com` (US1).

Legacy `DD_API_KEY` + `DD_APP_KEY` still works if no access token is set. The scanner loads `handover-dashboard/.env` first, then sibling `onboarding-readiness/.env` for `DD_*`.

Monitors are fetched with `GET /api/v1/monitor?monitor_tags=service:<name>`.

| Catalog name | Datadog tag |
|---|---|
| `task-mining` / `cloud-task-mining` | `service:task-mining` |
| `task-mining-ai` | `service:task-mining-ai` |
| `task-mining-uploader` | `service:task-mining-uploader` |
| `tm-image-collector` | `service:tm-image-collector` |

**Operate score** starts as `55% git + 45% live Datadog` (monitors, muted count, SLOs, 90-day incidents), then **runbook quality** is blended in (`55% that score + 45% quality`). Quality is strong vs thin “investigate” notes vs TODO stubs — not file count. Target is ~80% of live monitors with 5-minute recovery steps. Alerting monitors or SLO budget &lt; 10% are P0.

## Roadie scorecards (live catalog)

Pulls Tech Insights from [task-mining scorecards](https://celonis.roadie.so/catalog/default/component/task-mining/scorecards) (or the scanned component’s catalog name).

Set `ROADIE_API_TOKEN` in `.env` — create a User Token at [Roadie Administration](https://celonis.roadie.so/administration). See [Roadie API authorization](https://roadie.io/docs/api/authorization/).

Matched scorecards blend into handover areas as **55% git + 45% Roadie**. If none match an area, the headline score is **60% local + 40% Roadie average**.

## Confluence / Celospace (docs & runbooks)

Scores the [Task Mining hub](https://celonis-confluence.atlassian.net/wiki/spaces/DKB/pages/17674883/Task+Mining) on [celonis-confluence.atlassian.net](https://celonis-confluence.atlassian.net/) (space **DKB**): Knowledge Base, Way of Working, Squad Test Plan, Handover documents, how-tos.

Uses the same Atlassian API token as Jira (`JIRA_EMAIL` + `JIRA_TOKEN`). Override with `CONFLUENCE_EMAIL` / `CONFLUENCE_TOKEN` if needed. `CONFLUENCE_SITE=https://celonis-confluence.atlassian.net`.

Pages are classified strong / thin / stub from body length and title. SOPs blend into **Knowledge**, named runbook/SLO pages into **Operate**, architecture pages into **Architecture**. Pulse shows page / SOP / handover / freshness counts.

## Google Drive (docs & recordings)

Scores the [Task Mining squad shared Drive](https://drive.google.com/drive/folders/1fB6CtN8Gh2L0RtwBXv31Iy546H5BpkD4) when you scan a Task Mining repo.

Uses **Google Drive for Desktop** on this Mac (no API key). Open the folder once while signed in so it is cached.

**Knowledge** is then `55% git + 45% Drive`. Drive quality is the Feb 2026 KT recording pack vs empty/stale gdocs in `11 - Documentation` / `09 - Onboarding`. Recordings existing add **+2** on Documentation; ticking “KT walkthrough recorded” is **+3** (watched, not just filed).

To use a local copy of keys instead of the fallback: `cp .env.example .env` and fill in values. Do not commit `.env`.

## Limits

- Cannot score named on-call people or “game-day done”
- Datadog live scoring needs `DD_ACCESS_TOKEN` (or legacy `DD_API_KEY` / `DD_APP_KEY`)
- Roadie live scoring needs `ROADIE_API_TOKEN`
- Code Purple live metrics need `CELONIS_API_TOKEN` or `CELONIS_APP_KEY` on celocore.us-2
- GitHub live scoring needs `GITHUB_TOKEN`
- Ops that live in another repo (`cfg-ibc`, a client repo) will under-score this one
