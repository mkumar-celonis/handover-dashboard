const form = document.getElementById("scan-form");
const pathInput = document.getElementById("path");
const windowSelect = document.getElementById("window-days");
const scanBtn = document.getElementById("scan-btn");
const errorEl = document.getElementById("error");
const loadingEl = document.getElementById("loading");
const reportEl = document.getElementById("report");

let lastData = null;
let lastGroup = null;
let scanAbort = null;
const openDetails = new Set();
const DEFAULT_WEIGHTS = { shadow: 30, docs: 25, observe: 25, debt: 20 };
const DEFAULT_PASS = { docs: 20, observe: 20, shadow: 24, debt: 15 };
const WEIGHT_IDS = ["shadow", "docs", "observe", "debt"];

function weightsKey(path) {
  return `handover.weights.${path || "default"}`;
}

function loadWeights(path) {
  try {
    const raw = JSON.parse(localStorage.getItem(weightsKey(path)) || "null");
    if (!raw || typeof raw !== "object") return { ...DEFAULT_WEIGHTS };
    const next = { ...DEFAULT_WEIGHTS };
    WEIGHT_IDS.forEach((id) => {
      const n = Number(raw[id]);
      if (Number.isFinite(n)) next[id] = Math.max(0, Math.min(80, Math.round(n)));
    });
    return next;
  } catch {
    return { ...DEFAULT_WEIGHTS };
  }
}

function saveWeights(path, weights) {
  localStorage.setItem(weightsKey(path), JSON.stringify(weights));
}

function serviceScope(data) {
  if (lastGroup && data && data.service_id) return data.service_id;
  return (data && data.repo && data.repo.path) || data.service_id || currentPath();
}

function readSliderWeights(scope) {
  const root = document.querySelector(`[data-weight-scope="${CSS.escape(scope)}"]`);
  const next = { ...DEFAULT_WEIGHTS };
  (root ? root.querySelectorAll("[data-weight]") : []).forEach((el) => {
    next[el.dataset.weight] = Number(el.value);
  });
  return next;
}

function paintWeights(weights, scope) {
  const root = document.querySelector(`[data-weight-scope="${CSS.escape(scope)}"]`);
  if (!root) return;
  const total = WEIGHT_IDS.reduce((sum, id) => sum + Number(weights[id] || 0), 0);
  root.querySelectorAll("[data-weight]").forEach((el) => {
    el.value = String(weights[el.dataset.weight] ?? 0);
  });
  root.querySelectorAll("[data-weight-out]").forEach((el) => {
    el.textContent = String(weights[el.dataset.weightOut] ?? 0);
  });
  const totalEl = root.querySelector("[data-weight-total]");
  if (totalEl) totalEl.textContent = `${total} / 100`;
}

function adjustWeight(id, value, scope) {
  const current = readSliderWeights(scope);
  const nextVal = Math.max(0, Math.min(70, Math.round(Number(value) || 0)));
  const others = WEIGHT_IDS.filter((key) => key !== id);
  const oldRest = others.reduce((sum, key) => sum + Number(current[key] || 0), 0);
  const newRest = 100 - nextVal;
  const next = { ...current, [id]: nextVal };
  if (!others.length) return next;
  if (oldRest <= 0) {
    const even = Math.floor(newRest / others.length);
    others.forEach((key, idx) => {
      next[key] = idx === others.length - 1 ? newRest - even * (others.length - 1) : even;
    });
    return next;
  }
  let used = 0;
  others.forEach((key, idx) => {
    if (idx === others.length - 1) {
      next[key] = Math.max(0, newRest - used);
    } else {
      next[key] = Math.max(0, Math.round(newRest * (current[key] / oldRest)));
      used += next[key];
    }
  });
  return next;
}

function applyWeights(data, weights) {
  const pillars = (data.pillars || []).map((p) => {
    const max = Number(weights[p.id] ?? p.max);
    const ratio = p.git_ratio != null ? Number(p.git_ratio) : p.max ? Number(p.git_points || 0) / p.max : 0;
    const git_points = Math.round(ratio * max * 10) / 10;
    const pass = max === DEFAULT_WEIGHTS[p.id] ? DEFAULT_PASS[p.id] : Math.round(max * 0.8);
    const breakdown = (p.breakdown || []).map((row) => {
      if (row.area_id && row.weight != null) {
        const areaRatio = row.score != null ? Number(row.score) / 5 : 0;
        return {
          ...row,
          points: Math.round(areaRatio * row.weight * max * 10) / 10,
          max: Math.round(row.weight * max * 10) / 10,
        };
      }
      return row;
    });
    return { ...p, max, pass, git_points, breakdown };
  });
  const total = WEIGHT_IDS.reduce((sum, id) => sum + Number(weights[id] || 0), 0) || 100;
  return {
    ...data,
    pillars,
    handover: {
      ...(data.handover || {}),
      max: total,
      weights: { ...weights },
      weights_source: "ui",
      thresholds: { green: Math.round(total * 0.85), amber: Math.round(total * 0.7) },
    },
  };
}

function scoredView(data, path) {
  return applySignoff(applyWeights(data, loadWeights(path)), loadSignoff(path));
}

function scoredGroup(data) {
  const ready = (data.services || []).filter((svc) => !svc.pending);
  const pending = (data.services || []).length - ready.length;
  const services = ready.map((svc) => scoredView(svc, serviceScope(svc)));
  const points = services.map((s) => Number((s.handover || {}).points ?? s.overall_pct ?? 0));
  const avg = points.length ? Math.round(points.reduce((a, b) => a + b, 0) / points.length) : 0;
  const rank = { block: 0, conditional: 1, go: 2 };
  let worst = points.length ? "go" : "block";
  services.forEach((s) => {
    const v = (s.verdict || {}).verdict || "block";
    if ((rank[v] ?? 0) < (rank[worst] ?? 0)) worst = v;
  });
  const labels = { block: "Block handover", conditional: "Soft handover", go: "Complete handover" };
  const detail = pending
    ? `${services.length} of ${services.length + pending} scorecards ready.`
    : (data.verdict || {}).detail;
  return {
    ...data,
    services,
    pending,
    overall_pct: avg,
    overall_rag: worst === "block" ? "red" : worst === "conditional" ? "amber" : "green",
    verdict: {
      ...(data.verdict || {}),
      verdict: worst,
      label: pending ? "Scanning…" : labels[worst] || (data.verdict || {}).label,
      detail,
    },
    handover: { ...(data.handover || {}), points: avg, max: 100 },
  };
}

function isGroupInput(value) {
  const text = (value || "").trim().toLowerCase();
  return (
    text.startsWith("http://") ||
    text.startsWith("https://") ||
    text.startsWith("https:/") ||
    text.startsWith("group:") ||
    text === "task-mining" ||
    (text.includes("/catalog/") && text.includes("/group/"))
  );
}

function selectedKey(groupId) {
  return `handover.selected.${groupId || "default"}`;
}

function loadSelected(groupId, serviceIds) {
  try {
    const raw = JSON.parse(localStorage.getItem(selectedKey(groupId)) || "null");
    if (Array.isArray(raw) && raw.length) {
      return serviceIds.filter((id) => raw.includes(id));
    }
  } catch {
    /* defaults */
  }
  return [...serviceIds];
}

function saveSelected(groupId, ids) {
  localStorage.setItem(selectedKey(groupId), JSON.stringify(ids));
}

function pickerSelectedIds() {
  return [...document.querySelectorAll("[data-service-pick]:checked")].map((el) => el.value);
}

function collectPickerTunes() {
  const out = {};
  document.querySelectorAll(".picker-card").forEach((card) => {
    const id = card.dataset.serviceId;
    const box = card.querySelector("[data-service-pick]");
    if (!id || !box || !box.checked) return;
    out[id] = {
      path: card.dataset.servicePath || undefined,
      signoff: loadSignoff(id),
      weights: readSliderWeights(id),
    };
    saveWeights(id, out[id].weights);
  });
  return out;
}

function shadowBuckets(maximum) {
  const max = Math.max(0, Number(maximum) || 0);
  const observe = Math.floor(max / 3);
  const first = Math.floor(max / 3);
  return [observe, first, max - observe - first];
}

function shadowBreakdown(signoff, pillar) {
  const incidents = Number(signoff.reverse_incidents || 0);
  const [observe, first, second] = shadowBuckets(pillar.max);
  return [
    {
      label: "Incoming shadowed live incidents & deploys",
      points: signoff.primary_shadowing ? observe : 0,
      max: observe,
      done: Boolean(signoff.primary_shadowing),
      why: "Tick People sign-off. Git cannot prove shadowing.",
    },
    {
      label: "Incoming led 1 incident (outgoing backup)",
      points: incidents >= 1 ? first : 0,
      max: first,
      done: incidents >= 1,
      why: "First reverse-shadow incident without outgoing taking over.",
    },
    {
      label: "≥2 incidents without intervention",
      points: incidents >= 2 ? second : 0,
      max: second,
      done: incidents >= 2,
      why: `Passing threshold is ${pillar.pass}/${pillar.max} — need both reverse-shadow incidents.`,
    },
  ];
}

function applySignoff(data, signoff) {
  const pillars = (data.pillars || []).map((p) => {
    let earned = Number(p.git_points || 0);
    if (p.id === "shadow") {
      const [observe, first, second] = shadowBuckets(p.max);
      earned = 0;
      if (signoff.primary_shadowing) earned += observe;
      if (Number(signoff.reverse_incidents) >= 1) earned += first;
      if (Number(signoff.reverse_incidents) >= 2) earned += second;
    } else if (p.id === "docs") {
      if (signoff.kt_recorded) earned = Math.min(p.max, earned + 3);
      else if ((data.drive || {}).kt_ready) earned = Math.min(p.max, earned + 2);
    } else if (p.id === "observe") {
      if (signoff.dashboards_verified) earned = Math.min(p.max, earned + 2);
      if (signoff.access_transferred) earned = Math.min(p.max, earned + 3);
    }
    earned = Math.round(earned * 10) / 10;
    const below = earned < p.pass;
    const rag = below && earned < p.pass * 0.75 ? "red" : below ? "amber" : "green";
    let breakdown = (p.breakdown || [])
      .filter((row) => row.done == null)
      .map((row) => (row.area_id ? { ...row, why: "" } : row));
    if (p.id === "shadow") breakdown = shadowBreakdown(signoff, p);
    if (p.id === "docs" && signoff.kt_recorded) {
      breakdown = breakdown.concat([{ label: "KT walkthrough recorded", points: 3, max: 3, done: true, why: "People sign-off bonus." }]);
    } else if (p.id === "docs" && (data.drive || {}).kt_ready) {
      breakdown = breakdown.concat([{
        label: "Drive KT recordings exist",
        points: 2,
        max: 3,
        done: true,
        why: `${(data.drive || {}).kt_recordings || 0} handover recordings in Drive. Tick sign-off when incoming has watched them.`,
      }]);
    }
    if (p.id === "observe") {
      if (signoff.dashboards_verified) {
        breakdown = breakdown.concat([{ label: "Dashboards + logs verified", points: 2, max: 2, done: true, why: "People sign-off bonus." }]);
      }
      if (signoff.access_transferred) {
        breakdown = breakdown.concat([{ label: "Pager / repo / secrets access transferred", points: 3, max: 3, done: true, why: "People sign-off bonus." }]);
      }
    }
    return { ...p, earned, below_pass: below, rag, breakdown };
  });
  const points = Math.round(pillars.reduce((sum, p) => sum + p.earned, 0));
  const failing = pillars.filter((p) => p.below_pass).map((p) => p.label);
  const shadow = pillars.find((p) => p.id === "shadow") || { earned: 0, max: 30 };
  const total = Number((data.handover || {}).max) || pillars.reduce((sum, p) => sum + Number(p.max || 0), 0) || 100;
  const greenCut = Number((data.handover || {}).thresholds?.green) || Math.round(total * 0.85);
  const amberCut = Number((data.handover || {}).thresholds?.amber) || Math.round(total * 0.7);
  const [observeNeed] = shadowBuckets(shadow.max);
  const shadowOk = observeNeed === 0 || shadow.earned >= observeNeed;
  let verdict;
  if (points >= greenCut && failing.length === 0) {
    verdict = {
      verdict: "go",
      label: "Complete handover",
      detail: "Incoming team takes 100% operational ownership. Outgoing team disengages.",
      action: "Sign the cutover. Old team leaves the pager.",
    };
  } else if (points >= amberCut && shadowOk) {
    verdict = {
      verdict: "conditional",
      label: "Soft handover",
      detail:
        "Incoming team takes primary on-call. Outgoing stays on secondary escalation for 2 more weeks to close gaps" +
        (failing.length ? ` (${failing.join(", ")}).` : "."),
      action: "2-week bridge. Close failing pillars before outgoing leaves.",
    };
  } else {
    verdict = {
      verdict: "block",
      label: "Block handover",
      detail: `Transition halted. Escalate to leadership: ${failing.join(", ") || `score below ${amberCut}`}.`,
      action: "Do not accept support ownership. Send the failing pillars to leadership.",
    };
  }
  const gaps = (data.gaps || []).filter((g) => !String(g.id || "").startsWith("handover."));
  pillars
    .filter((p) => p.below_pass)
    .forEach((p) => {
      gaps.push({
        id: `handover.${p.id}`,
        title: `${p.label} below ${p.pass}/${p.max}`,
        ask: p.criteria,
        p0: p.id === "docs" || p.id === "observe" || p.id === "shadow" || p.earned < p.pass * 0.5,
      });
    });
  return {
    ...data,
    signoff,
    pillars,
    gaps,
    verdict,
    overall_pct: points,
    overall_rag: verdict.verdict === "block" ? "red" : verdict.verdict === "conditional" ? "amber" : "green",
    handover: { ...(data.handover || {}), points, failing, max: total, thresholds: { green: greenCut, amber: amberCut } },
  };
}

document.querySelectorAll("[data-path]").forEach((btn) => {
  btn.addEventListener("click", () => {
    pathInput.value = btn.dataset.path;
    updateScanButton();
    form.requestSubmit();
  });
});

const saved = localStorage.getItem("handover.path");
const fromQuery = new URLSearchParams(window.location.search).get("path");
const fromWindow = new URLSearchParams(window.location.search).get("w");
if (fromQuery) pathInput.value = fromQuery;
else if (saved) pathInput.value = saved;
const savedWindow = localStorage.getItem("handover.window_days");
if (fromWindow && windowSelect.querySelector(`option[value="${fromWindow}"]`)) {
  windowSelect.value = fromWindow;
} else if (savedWindow && windowSelect.querySelector(`option[value="${savedWindow}"]`)) {
  windowSelect.value = savedWindow;
}

function signoffKey(path) {
  return `handover.signoff.${path}`;
}

function loadSignoff(path) {
  try {
    return {
      kt_recorded: false,
      dashboards_verified: false,
      access_transferred: false,
      primary_shadowing: false,
      reverse_incidents: 0,
      ...(JSON.parse(localStorage.getItem(signoffKey(path)) || "{}") || {}),
    };
  } catch {
    return {
      kt_recorded: false,
      dashboards_verified: false,
      access_transferred: false,
      primary_shadowing: false,
      reverse_incidents: 0,
    };
  }
}

function saveSignoff(path, signoff) {
  localStorage.setItem(signoffKey(path), JSON.stringify(signoff));
}

function updateScanButton() {
  scanBtn.textContent = isGroupInput(pathInput.value) ? "Load services" : "Scan";
}

pathInput.addEventListener("input", updateScanButton);
updateScanButton();

async function parseResponse(res) {
  const raw = await res.text();
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    throw new Error(raw.slice(0, 200) || res.statusText);
  }
  if (!res.ok) throw new Error(data.detail || data.message || res.statusText);
  return data;
}

async function loadGroupCatalog(path) {
  const res = await fetch(`/api/group?ref=${encodeURIComponent(path)}`);
  const data = await parseResponse(res);
  lastGroup = data;
  lastData = null;
  renderGroupWorkspace();
}

function upsertService(report) {
  if (!lastData || lastData.kind !== "group") return;
  const id = report.service_id;
  const idx = (lastData.services || []).findIndex((s) => s.service_id === id);
  if (idx >= 0) lastData.services[idx] = report;
  else lastData.services.push(report);
}

function setScanBusy(busy, label) {
  scanBtn.disabled = busy;
  const submit = document.getElementById("scan-selected");
  if (submit) submit.disabled = busy;
  if (busy) {
    loadingEl.hidden = false;
    loadingEl.textContent = label || "Scanning…";
  } else {
    loadingEl.hidden = true;
    loadingEl.textContent = "Scanning…";
  }
}

async function scanSelectedServices() {
  const selected = pickerSelectedIds();
  if (!selected.length) {
    errorEl.hidden = false;
    errorEl.textContent = "Select at least one service.";
    return;
  }
  errorEl.hidden = true;
  if (scanAbort) scanAbort.abort();
  scanAbort = new AbortController();
  const signal = scanAbort.signal;
  const tunes = collectPickerTunes();
  const windowDays = Number(windowSelect.value || 90);
  const catalog = lastGroup.services || [];
  lastData = {
    kind: "group",
    group: {
      id: lastGroup.id,
      title: lastGroup.title,
      url: lastGroup.url,
      source: lastGroup.source,
      service_count: selected.length,
    },
    window_days: windowDays,
    services: selected.map((id) => {
      const meta = catalog.find((s) => s.id === id) || {};
      return {
        service_id: id,
        service_title: meta.title || id,
        service_url: meta.url,
        pending: true,
      };
    }),
  };
  refreshGroupResults();
  setScanBusy(true, `Scanning 0 of ${selected.length}…`);
  let done = 0;
  const jobs = selected.map(async (id) => {
    const meta = catalog.find((s) => s.id === id) || {};
    const tune = tunes[id] || {};
    try {
      const report = await parseResponse(
        await fetch("/api/scan/service", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          signal,
          body: JSON.stringify({
            service_id: id,
            path: tune.path || meta.path || undefined,
            title: meta.title,
            url: meta.url,
            service_type: meta.type,
            github_slug: meta.github_slug || undefined,
            window_days: windowDays,
            signoff: tune.signoff,
            weights: tune.weights,
          }),
        })
      );
      upsertService(report);
    } catch (err) {
      if (err.name === "AbortError") return;
      upsertService({
        service_id: id,
        service_title: meta.title || id,
        service_url: meta.url,
        error: err.message || String(err),
        overall_pct: 0,
        overall_rag: "red",
        verdict: { verdict: "block", label: "Scan failed", detail: err.message || String(err) },
        handover: { points: 0, max: 100 },
        repo: { path: meta.path || "", name: meta.title || id, catalog: {}, git: {} },
      });
    }
    done += 1;
    setScanBusy(true, `Scanning ${done} of ${selected.length}…`);
    refreshGroupResults();
  });
  try {
    await Promise.all(jobs);
  } finally {
    if (!signal.aborted) setScanBusy(false);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const path = pathInput.value.trim();
  if (!path) return;
  localStorage.setItem("handover.path", path);
  localStorage.setItem("handover.window_days", String(windowSelect.value || 90));
  errorEl.hidden = true;
  reportEl.hidden = true;
  loadingEl.hidden = false;
  scanBtn.disabled = true;
  try {
    if (isGroupInput(path)) {
      loadingEl.textContent = "Loading group services…";
      await loadGroupCatalog(path);
      return;
    }
    loadingEl.textContent = "Scanning…";
    const data = await parseResponse(
      await fetch("/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path,
          window_days: Number(windowSelect.value || 90),
          signoff: loadSignoff(path),
          weights: loadWeights(path),
        }),
      })
    );
    lastGroup = null;
    lastData = data;
    render(scoredView(data, path));
  } catch (err) {
    errorEl.hidden = false;
    errorEl.textContent = err.message || String(err);
  } finally {
    loadingEl.hidden = true;
    loadingEl.textContent = "Scanning…";
    scanBtn.disabled = false;
  }
});

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function liveLine(label, item, href, { brief } = {}) {
  if (!item) return "";
  const rag = item.rag || "amber";
  const score = item.score != null ? `${item.score}/5` : "—";
  const title = item.title || item.status || "";
  const note = brief
    ? item.summary || ((item.why || [])[0] || "")
    : [item.summary, ...((item.why || []).slice(0, 2))].filter(Boolean)[0] || "";
  const head = href
    ? `<a class="linkish" href="${esc(href)}" target="_blank" rel="noreferrer">${esc(label)}</a>`
    : esc(label);
  return `<li class="live-card">
    <div class="live-top">
      <span class="live-label">${head}</span>
      <span class="rag ${esc(rag)}">${esc(rag)}</span>
    </div>
    <strong class="live-score">${esc(score)}</strong>
    <p class="meta">${esc(title)}${note && title ? " · " : ""}${esc(note)}</p>
  </li>`;
}

function pulseHtml(data) {
  const gh = (data.github || {}).signals || {};
  const e2e = ((data.github || {}).e2e || {}).signals || {};
  const jira = (data.jira || {}).signals || {};
  const dd = (data.datadog || {}).signals || {};
  const conf = (data.confluence || {}).signals || {};
  const ghLinks = gh.links || {};
  const jiraLinks = (data.jira || {}).links || {};
  const ddLinks = (data.datadog || {}).links || {};
  const confLinks = (data.confluence || {}).links || {};
  const cell = (label, value, href) => {
    const text = value == null || value === "" ? "—" : String(value);
    const val = href
      ? `<a class="linkish" href="${esc(href)}" target="_blank" rel="noreferrer">${esc(text)}</a>`
      : `<strong>${esc(text)}</strong>`;
    return `<div class="metric"><span class="meta">${esc(label)}</span>${val}</div>`;
  };
  const cov = gh.coverage_pct != null ? `${gh.coverage_pct}%` : "—";
  return `<div class="pulse-grid">
    <div class="pulse-block">
      <p class="pulse-label">GitHub</p>
      <div class="metrics">
        ${cell("Test files", gh.test_files, ghLinks.test_files)}
        ${cell("E2E files", gh.e2e_files, ghLinks.e2e_files)}
        ${cell("E2E runs", e2e.runs_in_window, e2e.runs_url || e2e.actions_url || e2e.last_url)}
        ${cell("Coverage", cov, ghLinks.coverage)}
        ${cell("Merged PRs", gh.merged_prs, ghLinks.merged_prs)}
        ${cell("Reverts", gh.reverted_prs, ghLinks.reverted_prs)}
        ${cell("Regression PRs", gh.regression_prs, ghLinks.regression_prs)}
      </div>
    </div>
    <div class="pulse-block">
      <p class="pulse-label">Jira</p>
      <div class="metrics">
        ${cell("CBE", jira.cbe, jiraLinks.cbe)}
        ${cell("Security", jira.security, jiraLinks.security)}
        ${cell("Features", jira.features, jiraLinks.features)}
        ${cell("Bugs", jira.bugs, jiraLinks.bugs)}
      </div>
    </div>
    <div class="pulse-block">
      <p class="pulse-label">Datadog</p>
      <div class="metrics">
        ${cell("P0 / Sev-1", dd.p0 ?? dd.sev1_90d, ddLinks.p0)}
        ${cell("Alerts", dd.alerts ?? dd.alerting, ddLinks.alerts || ddLinks.alerting)}
        ${cell("Monitors", dd.monitors, ddLinks.monitors)}
        ${cell("Incidents", dd.incidents_90d, ddLinks.incidents)}
      </div>
    </div>
    <div class="pulse-block">
      <p class="pulse-label">Confluence</p>
      <div class="metrics">
        ${cell("Pages", conf.pages, confLinks.pages || confLinks.hub)}
        ${cell("SOPs / how-tos", conf.sops, confLinks.sops)}
        ${cell("Runbook / SLO", conf.runbooks, confLinks.runbooks)}
        ${cell("Handover pages", conf.handover, confLinks.handover)}
        ${cell("Updated in window", conf.updated, confLinks.updated)}
        ${cell("Stubs", conf.stubs, confLinks.pages)}
      </div>
    </div>
  </div>`;
}

function areaBrief(area) {
  const extras = [];
  if (area.runbook_quality != null) extras.push(`runbooks ${area.runbook_quality}/5`);
  if (area.drive_score != null) extras.push(`Drive ${area.drive_score}/5`);
  if (area.confluence_score != null) extras.push(`Confluence ${area.confluence_score}/5`);
  if (area.confluence_runbook_score != null) extras.push(`Celospace runbooks ${area.confluence_runbook_score}/5`);
  if (area.datadog_score != null) extras.push(`Datadog ${area.datadog_score}/5`);
  if (area.roadie_score != null) extras.push(`Roadie ${area.roadie_score}/5`);
  if (area.github_score != null) extras.push(`GitHub ${area.github_score}/5`);
  if (area.e2e_score != null) extras.push(`E2E ${area.e2e_score}/5`);
  return extras.join(" · ");
}

function weightCaption(data) {
  const ho = data.handover || {};
  const w = ho.weights || {};
  const green = (ho.thresholds || {}).green ?? 85;
  const amber = (ho.thresholds || {}).amber ?? 70;
  const parts = ["docs", "observe", "shadow", "debt"]
    .filter((id) => w[id] != null)
    .map((id) => `${id} ${w[id]}`);
  const risk = parts.length
    ? `Weighted by risk, not evenly (${parts.join(" · ")}). `
    : "Weighted by risk, not evenly: reverse shadowing 30, docs 25, observe 25, debt 20. ";
  const source = ho.weights_source === "ui"
    ? "Tuned with the sliders (saved for this repo). "
    : ho.weights_source && ho.weights_source !== "dashboard" && ho.weights_source !== "default"
    ? `Tuned from ${ho.weights_source}. `
    : "Drag the sliders above to retune this service. ";
  return `${risk}${source}Green ≥${green} and all pillars pass. Amber ${amber}–${green - 1}: incoming primary, outgoing secondary 2 weeks. Below ${amber}: block.`;
}

function pillarWhy(p) {
  const rows = (p.breakdown || [])
    .map((row) => {
      const score = row.score != null ? `${row.score}/5` : `${row.points}/${row.max}`;
      const rag = row.rag || (row.done ? "green" : "red");
      const why = row.area_id ? "" : row.why;
      return `<div class="why-row">
        <div class="why-head">
          <strong>${esc(row.label)}</strong>
          <span class="rag ${esc(rag)}">${esc(rag)}</span>
          <span class="meta">${esc(score)}${row.points != null && row.score != null ? ` · ${row.points}/${row.max} pts` : ""}</span>
        </div>
        ${why ? `<p class="meta">${esc(why)}</p>` : ""}
      </div>`;
    })
    .join("");
  return `<div class="why">${rows || "<p class='meta'>No breakdown.</p>"}</div>`;
}

function weightsPanelHtml(scope, weights, { compact } = {}) {
  const w = weights || loadWeights(scope);
  const total = WEIGHT_IDS.reduce((sum, id) => sum + Number(w[id] || 0), 0);
  const rows = [
    ["shadow", compact ? "Shadow" : "Reverse shadowing"],
    ["docs", compact ? "Docs" : "Documentation"],
    ["observe", compact ? "Observe" : "Observability"],
    ["debt", compact ? "Debt" : "Backlog & tech debt"],
  ]
    .map(
      ([id, label]) => `<label class="weight-row">
        <span>${esc(label)}</span>
        <input type="range" data-weight="${id}" min="0" max="70" step="1" value="${w[id]}" />
        <output data-weight-out="${id}">${w[id]}</output>
      </label>`
    )
    .join("");
  return `<section class="weights${compact ? " compact" : ""}" data-weight-scope="${esc(scope)}">
    <div class="weights-head">
      <strong>Score weights</strong>
      <span class="meta" data-weight-total>${total} / 100</span>
      <button type="button" class="linkish" data-weight-reset>Reset</button>
    </div>
    <div class="weight-grid">${rows}</div>
    ${compact ? "" : "<p class='meta'>Risk-weighted for this service. Dragging one slider keeps the total at 100.</p>"}
  </section>`;
}

function serviceCardHtml(data, { nested } = {}) {
  const repo = data.repo || {};
  const catalog = repo.catalog || {};
  const v = data.verdict || {};
  const plat = data.platform || {};
  const dd = data.datadog || {};
  const roadie = data.roadie || {};
  const scope = serviceScope(data);
  const signoff = { ...loadSignoff(scope), ...(data.signoff || {}) };
  const handover = data.handover || {};
  const prefix = nested ? `${data.service_id || scope}-` : "";

  const chips = [];
  if (data.service_type) chips.push(data.service_type);
  if (catalog.owner) chips.push(catalog.owner);
  if (catalog.tier) chips.push(`tier ${catalog.tier}`);
  if (catalog.lifecycle) chips.push(catalog.lifecycle);
  if (data.missing_clone) chips.push("no local clone");
  if (data.window_days) chips.push(`window ${data.window_days}d`);

  const p0 = (data.gaps || []).filter((g) => g.p0);
  const gapHtml = p0.length
    ? p0
        .map(
          (g) => `<article class="gap">
            <strong><span class="tag p0">P0</span>${esc(g.title)}</strong>
            ${esc(g.ask)}
          </article>`
        )
        .join("")
    : "<p class='meta'>No P0 gaps.</p>";

  const pillarRows = (data.pillars || [])
    .map((p) => {
      const pct = Math.round((p.earned / p.max) * 100);
      const open = openDetails.has(`pillar-${prefix}${p.id}`) ? "open" : "";
      return `<tr>
        <td colspan="3">
          <details class="reveal" data-id="pillar-${esc(prefix)}${esc(p.id)}" ${open}>
            <summary>
              <span>
                ${esc(p.label)}
                <div class="bar ${p.rag}"><span style="width:${pct}%"></span></div>
                <div class="meta">${esc(p.criteria)}</div>
              </span>
              <span class="score-cell">${p.earned}/${p.max}</span>
              <span class="rag ${p.rag}">${p.below_pass ? "fail " + p.pass : "pass " + p.pass}</span>
            </summary>
            ${pillarWhy(p)}
          </details>
        </td>
      </tr>`;
    })
    .join("");

  const areaRows = (data.areas || [])
    .map((area) => {
      const open = openDetails.has(`area-${prefix}${area.id}`) ? "open" : "";
      const checks = (area.checks || [])
        .map((c) => `<li>
            <span class="status ${esc(c.status)}">${esc(c.status)}</span>
            ${esc(c.evidence)}
          </li>`)
        .join("");
      return `<tr>
        <td colspan="3">
          <details class="reveal" data-id="area-${esc(prefix)}${esc(area.id)}" ${open}>
            <summary>
              <span>
                ${esc(area.label)}${area.p0 ? " · P0" : ""}
                <div class="bar ${area.rag}"><span style="width:${area.score_pct}%"></span></div>
              </span>
              <span class="score-cell">${area.score}/5</span>
              <span class="rag ${area.rag}">${esc(area.rag)}</span>
            </summary>
            ${areaBrief(area) ? `<p class="meta">${esc(areaBrief(area))}</p>` : ""}
            ${checks ? `<ul class="check-list">${checks}</ul>` : ""}
          </details>
        </td>
      </tr>`;
    })
    .join("");

  const liveHtml = [
    liveLine("GitHub", data.github || {}, (data.github || {}).url, { brief: nested }),
    liveLine("E2E / tests", (data.github || {}).e2e, ((data.github || {}).e2e || {}).url, { brief: nested }),
    liveLine("Roadie", roadie, roadie.url, { brief: nested }),
    liveLine("Datadog", dd, (dd.links || {}).monitors, { brief: nested }),
    liveLine("Drive", data.drive || {}, (data.drive || {}).url, { brief: nested }),
    liveLine("Confluence", data.confluence || {}, (data.confluence || {}).url, { brief: nested }),
    liveLine("Runbooks", data.runbooks || {}, undefined, { brief: nested }),
    liveLine("Kargo", plat.kargo, (plat.kargo || {}).url, { brief: nested }),
    liveLine("Code Purple", plat.code_purple, (plat.code_purple || {}).url, { brief: nested }),
    liveLine("Jira", data.jira, (data.jira || {}).url, { brief: nested }),
  ].join("");

  const checked = (key) => (signoff[key] ? "checked" : "");
  const title = data.service_title || repo.name || data.service_id || "Service";
  const link = data.service_url
    ? ` <a class="linkish" href="${esc(data.service_url)}" target="_blank" rel="noreferrer">Roadie</a>`
    : "";
  const err = data.error ? `<p class="error">${esc(data.error)}</p>` : "";
  const w = handover.weights || {};

  return `<article class="service-card${nested ? " nested" : ""}" data-service-scope="${esc(scope)}">
    <section class="verdict ${esc(v.verdict === "block" ? "red" : v.verdict === "conditional" ? "amber" : "green")}">
      <div class="svc-head">
        <div>
          <p class="eyebrow">${esc(title)}${link}</p>
          <h2>${esc(v.label || "Scorecard")}</h2>
        </div>
        <p class="pct">${handover.points ?? data.overall_pct ?? 0}<span class="meta"> / ${handover.max || 100}</span></p>
      </div>
      ${nested ? "" : `<p>${esc(v.detail || "")}</p>${v.action ? `<p class="meta">${esc(v.action)}</p>` : ""}`}
      ${err}
      <div class="chips">${chips.map((c) => `<span class="chip">${esc(c)}</span>`).join("")}</div>
    </section>
    ${nested ? "" : weightsPanelHtml(scope, handover.weights)}
    <div class="panel-grid">
      <section class="panel">
        <h3>Exit scorecard</h3>
        <table>
          <tbody>${pillarRows}</tbody>
        </table>
        <p class="meta">${nested ? `Weights ${w.shadow ?? 30}/${w.docs ?? 25}/${w.observe ?? 25}/${w.debt ?? 20}` : weightCaption(data)}</p>
      </section>
      <section class="panel">
        <h3>Git areas</h3>
        <table>
          <tbody>${areaRows}</tbody>
        </table>
      </section>
      <section class="panel">
        <h3>People sign-off</h3>
        <ul class="signoff">
          <li><label><input type="checkbox" data-so="kt_recorded" ${checked("kt_recorded")}> KT recorded${(data.drive || {}).kt_ready ? ` <span class="meta">(${(data.drive || {}).kt_recordings || 0})</span>` : ""}</label></li>
          <li><label><input type="checkbox" data-so="dashboards_verified" ${checked("dashboards_verified")}> Dashboards verified</label></li>
          <li><label><input type="checkbox" data-so="access_transferred" ${checked("access_transferred")}> Access transferred</label></li>
          <li><label><input type="checkbox" data-so="primary_shadowing" ${checked("primary_shadowing")}> Primary shadowing</label></li>
          <li>
            <label>Reverse shadowing
              <select data-so="reverse_incidents">
                <option value="0" ${signoff.reverse_incidents == 0 ? "selected" : ""}>None</option>
                <option value="1" ${signoff.reverse_incidents == 1 ? "selected" : ""}>1 incident</option>
                <option value="2" ${signoff.reverse_incidents >= 2 ? "selected" : ""}>≥2</option>
              </select>
            </label>
          </li>
        </ul>
      </section>
      <section class="panel">
        <details class="panel-fold" data-id="blockers-${esc(prefix)}" ${openDetails.has(`blockers-${prefix}`) ? "open" : ""}>
          <summary>
            <h3>Blockers</h3>
            <span class="meta">${p0.length ? `${p0.length} P0` : "None"}</span>
          </summary>
          ${gapHtml}
        </details>
      </section>
      <section class="panel wide">
        <h3>Live pulse</h3>
        ${pulseHtml(data)}
      </section>
      <section class="panel wide">
        <h3>Live sources</h3>
        <ul class="live">${liveHtml}</ul>
      </section>
    </div>
  </article>`;
}

function refreshView() {
  if (lastGroup) {
    renderGroupWorkspace();
    return;
  }
  if (!lastData) return;
  render(scoredView(lastData, serviceScope(lastData)));
}

function groupPickerHtml(group) {
  const services = group.services || [];
  const chosen = new Set(loadSelected(group.id, services.map((s) => s.id)));
  const rows = services
    .map((svc) => {
      const checked = chosen.has(svc.id) ? "checked" : "";
      const dim = chosen.has(svc.id) ? "" : " unselected";
      const clone = svc.path
        ? `<span class="meta">${esc(svc.path)}</span>`
        : `<span class="meta">No local clone — live sources only</span>`;
      const link = svc.url
        ? ` <a class="linkish" href="${esc(svc.url)}" target="_blank" rel="noreferrer">Roadie</a>`
        : "";
      return `<article class="picker-card${dim}" data-service-id="${esc(svc.id)}" data-service-path="${esc(svc.path || "")}">
        <label class="picker-head">
          <input type="checkbox" data-service-pick value="${esc(svc.id)}" ${checked} />
          <span>
            <strong>${esc(svc.title || svc.id)}</strong>
            ${link}
            <div class="meta">${esc(svc.id)}${svc.type ? ` · ${esc(svc.type)}` : ""}</div>
            ${clone}
          </span>
        </label>
        ${weightsPanelHtml(svc.id, loadWeights(svc.id), { compact: true })}
      </article>`;
    })
    .join("");
  return `<section class="group-picker">
    <div class="weights-head">
      <strong>${esc(group.title || "Group")}</strong>
      <span class="meta">${services.length} services</span>
      ${group.url ? `<a class="linkish" href="${esc(group.url)}" target="_blank" rel="noreferrer">Roadie group</a>` : ""}
      <button type="button" class="linkish" id="select-all">Select all</button>
      <button type="button" class="linkish" id="select-none">Select none</button>
    </div>
    <p class="meta">Select services and set score weights. Each scorecard appears as soon as that service finishes.</p>
    <div class="picker-grid">${rows}</div>
    <div class="actions">
      <button type="button" id="scan-selected">Scan selected services</button>
    </div>
  </section>`;
}

function bindGroupPicker() {
  const persistPicks = () => {
    if (!lastGroup) return;
    const ids = pickerSelectedIds();
    saveSelected(lastGroup.id, ids);
    document.querySelectorAll(".picker-card").forEach((card) => {
      const box = card.querySelector("[data-service-pick]");
      card.classList.toggle("unselected", !(box && box.checked));
    });
  };
  document.querySelectorAll("[data-service-pick]").forEach((el) => {
    el.addEventListener("change", persistPicks);
  });
  document.getElementById("select-all")?.addEventListener("click", () => {
    document.querySelectorAll("[data-service-pick]").forEach((el) => {
      el.checked = true;
    });
    persistPicks();
  });
  document.getElementById("select-none")?.addEventListener("click", () => {
    document.querySelectorAll("[data-service-pick]").forEach((el) => {
      el.checked = false;
    });
    persistPicks();
  });
  document.querySelectorAll(".picker-card").forEach((card) => {
    const scope = card.dataset.serviceId;
    card.querySelectorAll("[data-weight]").forEach((el) => {
      el.addEventListener("input", () => {
        const next = adjustWeight(el.dataset.weight, el.value, scope);
        paintWeights(next, scope);
        saveWeights(scope, next);
        refreshGroupResults();
      });
    });
    card.querySelector("[data-weight-reset]")?.addEventListener("click", () => {
      saveWeights(scope, { ...DEFAULT_WEIGHTS });
      paintWeights(DEFAULT_WEIGHTS, scope);
      refreshGroupResults();
    });
  });
  document.getElementById("scan-selected")?.addEventListener("click", scanSelectedServices);
}

function pendingCardHtml(svc) {
  return `<article class="service-card nested pending">
    <section class="verdict">
      <div class="svc-head">
        <div>
          <p class="eyebrow">${esc(svc.service_title || svc.service_id)}</p>
          <h2>Scanning…</h2>
        </div>
        <p class="meta">Fetching scorecard</p>
      </div>
    </section>
  </article>`;
}

function groupResultsHtml() {
  if (!lastData || lastData.kind !== "group") return "";
  const scored = scoredGroup(lastData);
  const v = scored.verdict || {};
  const byId = Object.fromEntries((scored.services || []).map((s) => [s.service_id, s]));
  const cards = (lastData.services || [])
    .map((svc) => {
      if (svc.pending) return pendingCardHtml(svc);
      const ready = byId[svc.service_id] || scoredView(svc, serviceScope(svc));
      return serviceCardHtml(ready, { nested: true });
    })
    .join("");
  return `<section class="verdict group-summary ${esc(v.verdict === "block" ? "red" : v.verdict === "conditional" ? "amber" : "green")}">
        <div class="svc-head">
          <div>
            <p class="eyebrow">${esc((scored.group || {}).title || lastGroup.title || "Group")}</p>
            <h2>${esc(v.label || "")}</h2>
            <p class="meta">${esc(v.detail || "")}</p>
          </div>
          <p class="pct">${scored.overall_pct ?? 0}<span class="meta"> / 100</span></p>
        </div>
        <div class="actions">
          <button type="button" id="copy-md">Copy report</button>
        </div>
      </section>
      <div class="results-grid">${cards}</div>`;
}

function bindGroupResults() {
  const root = document.getElementById("group-results");
  if (!root) return;
  root.querySelectorAll("details.reveal, details.panel-fold").forEach((el) => {
    el.addEventListener("toggle", () => {
      const id = el.dataset.id;
      if (!id) return;
      if (el.open) openDetails.add(id);
      else openDetails.delete(id);
    });
  });
  root.querySelectorAll(".service-card").forEach(bindServiceCard);
  const md = groupMarkdown();
  document.getElementById("copy-md")?.addEventListener("click", async () => {
    await navigator.clipboard.writeText(md);
    document.getElementById("copy-md").textContent = "Copied";
    setTimeout(() => (document.getElementById("copy-md").textContent = "Copy report"), 1500);
  });
}

function groupMarkdown() {
  if (!lastData) return "";
  const scored = scoredGroup(lastData);
  const g = scored.group || lastGroup || {};
  const v = scored.verdict || {};
  const lines = [
    `# Handover readiness: ${g.title || "Group"}`,
    "",
    `**Verdict:** ${v.label || ""} · **${scored.overall_pct}/100**`,
    v.detail || "",
    "",
  ];
  (scored.services || []).forEach((svc) => {
    const ho = svc.handover || {};
    const vv = svc.verdict || {};
    lines.push(`## ${svc.service_title || svc.service_id}`);
    lines.push(`- Score: ${ho.points ?? svc.overall_pct}/${ho.max || 100} · ${vv.label || ""}`);
    if (svc.error) lines.push(`- Error: ${svc.error}`);
    lines.push("");
  });
  return lines.join("\n");
}

function refreshGroupResults() {
  let root = document.getElementById("group-results");
  if (!root) {
    if (!lastGroup) return;
    renderGroupWorkspace();
    return;
  }
  root.innerHTML = groupResultsHtml();
  bindGroupResults();
}

function renderGroupWorkspace() {
  if (!lastGroup) return;
  reportEl.innerHTML = `${groupPickerHtml(lastGroup)}<div id="group-results">${groupResultsHtml()}</div>`;
  reportEl.hidden = false;
  bindGroupPicker();
  bindGroupResults();
}

function bindServiceCard(card) {
  const scope = card.dataset.serviceScope;
  card.querySelectorAll("[data-so]").forEach((el) => {
    el.addEventListener("change", () => {
      const next = loadSignoff(scope);
      card.querySelectorAll("[data-so]").forEach((inner) => {
        if (inner.type === "checkbox") next[inner.dataset.so] = inner.checked;
        else next[inner.dataset.so] = Number(inner.value);
      });
      saveSignoff(scope, next);
      if (lastGroup) refreshGroupResults();
      else refreshView();
    });
  });
  card.querySelectorAll("[data-weight]").forEach((el) => {
    el.addEventListener("input", () => {
      const next = adjustWeight(el.dataset.weight, el.value, scope);
      paintWeights(next, scope);
      saveWeights(scope, next);
      refreshView();
    });
  });
  card.querySelector("[data-weight-reset]")?.addEventListener("click", () => {
    saveWeights(scope, { ...DEFAULT_WEIGHTS });
    paintWeights(DEFAULT_WEIGHTS, scope);
    refreshView();
  });
}

function render(data) {
  if (data.kind === "group" || lastGroup) {
    renderGroupWorkspace();
    return;
  }
  reportEl.innerHTML = `
    ${serviceCardHtml(data, { nested: false })}
    <div class="actions" style="margin-top:12px"><button type="button" id="copy-md">Copy report</button></div>
  `;
  reportEl.hidden = false;
  reportEl.querySelectorAll("details.reveal, details.panel-fold").forEach((el) => {
    el.addEventListener("toggle", () => {
      const id = el.dataset.id;
      if (!id) return;
      if (el.open) openDetails.add(id);
      else openDetails.delete(id);
    });
  });
  reportEl.querySelectorAll(".service-card").forEach(bindServiceCard);
  const md = data.markdown || "";
  document.getElementById("copy-md")?.addEventListener("click", async () => {
    await navigator.clipboard.writeText(md);
    document.getElementById("copy-md").textContent = "Copied";
    setTimeout(() => (document.getElementById("copy-md").textContent = "Copy report"), 1500);
  });
}

function currentPath() {
  return pathInput.value.trim();
}

function ragCell(text) {
  const raw = String(text || "").trim();
  if (!raw || raw === "—") return "—";
  const lower = raw.toLowerCase();
  const cls = lower.startsWith("green") || lower.includes("informational")
    ? "green"
    : lower.startsWith("amber") || lower.startsWith("3.0")
      ? "amber"
      : lower.startsWith("red") || lower.startsWith("<")
        ? "red"
        : "";
  return cls ? `<span class="rag ${cls}">${esc(raw)}</span>` : esc(raw);
}

function criteriaSectionHtml(section) {
  const rows = (section.rows || [])
    .map((row) => {
      const checks = (row.checks || [])
        .map(
          (c) => `<li>
            <strong>${esc(c.evidence)}</strong>
            ${c.p0 || c.escalate ? `<span class="tag">P0 if missing</span>` : ""}
            <div class="meta">${esc(c.how || "")}${c.weight != null ? ` · weight ${esc(c.weight)}` : ""}</div>
          </li>`
        )
        .join("");
      return `<tr>
        <td>
          <strong>${esc(row.criterion)}${row.p0 ? ` <span class="tag">P0</span>` : ""}</strong>
          ${checks ? `<ul class="check-list">${checks}</ul>` : ""}
        </td>
        <td>${esc(row.mechanism || "")}</td>
        <td>${ragCell(row.green)}</td>
        <td>${ragCell(row.amber)}</td>
        <td>${ragCell(row.red)}</td>
      </tr>`;
    })
    .join("");
  return `<details class="criteria-section" open>
    <summary>${esc(section.title)}</summary>
    ${section.intro ? `<p class="meta">${esc(section.intro)}</p>` : ""}
    <table class="criteria-table">
      <thead>
        <tr><th>Criterion</th><th>How we score it</th><th>Green</th><th>Amber</th><th>Red</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  </details>`;
}

function execBriefHtml(brief) {
  if (!brief) return "";
  const decisions = (brief.decision || [])
    .map(
      (d) => `<article class="exec-card ${esc(d.rag)}">
        <p class="eyebrow"><span class="rag ${esc(d.rag)}">${esc(d.label)}</span></p>
        <p class="exec-when">${esc(d.when)}</p>
        <p>${esc(d.means)}</p>
      </article>`
    )
    .join("");
  const pillars = (brief.pillars || [])
    .map(
      (p) => `<tr>
        <td><strong>${esc(p.label)}</strong></td>
        <td>${esc(p.weight)}</td>
        <td>${esc(p.pass)}</td>
        <td>${esc(p.ask)}</td>
      </tr>`
    )
    .join("");
  const sources = (brief.sources || [])
    .map((s) => `<li><strong>${esc(s.name)}</strong><span class="meta">${esc(s.role)}</span></li>`)
    .join("");
  return `
    <p class="exec-q">${esc(brief.question || "")}</p>
    <div class="exec-grid">${decisions}</div>
    <table class="exec-table">
      <thead><tr><th>Pillar</th><th>Weight</th><th>Pass</th><th>What we ask</th></tr></thead>
      <tbody>${pillars}</tbody>
    </table>
    <p class="meta">${esc(brief.rag || "")}</p>
    ${brief.group ? `<p class="meta">${esc(brief.group)}</p>` : ""}
    <ul class="exec-sources">${sources}</ul>
  `;
}

function renderCriteria(data) {
  const root = document.getElementById("criteria-body");
  if (!root || !data) return;
  const detail = `
    <p class="meta">${esc(data.summary || "")}</p>
    ${(data.sections || []).map(criteriaSectionHtml).join("")}
  `;
  root.innerHTML = `
    ${execBriefHtml(data.exec)}
    <details class="criteria-section">
      <summary>Engineering detail</summary>
      ${detail}
    </details>
  `;
}

async function loadCriteria() {
  const root = document.getElementById("criteria-body");
  try {
    const res = await fetch("/api/criteria");
    if (!res.ok) throw new Error("Could not load criteria");
    renderCriteria(await res.json());
  } catch (err) {
    if (root) root.innerHTML = `<p class="error">${esc(err.message || err)}</p>`;
  }
}

loadCriteria();

if (fromQuery && pathInput.value.trim()) {
  form.requestSubmit();
}
