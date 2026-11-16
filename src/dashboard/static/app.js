"use strict";

const byId = (id) => document.getElementById(id);
const number = (value) => value == null ? "Unknown" : Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
const money = (value) => value == null ? "Unknown" : `$${Number(value).toFixed(2)}`;
let busy = false;
let refreshing = false;
let revision = 0;
let recommendationsReady = false;
let analysisReadiness = null;
let actionNotice = "";
const executionNotices = new Map();

function element(tag, text, className) {
  const node = document.createElement(tag);
  // All API/model strings are text, never executable HTML.
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function request(path, method = "GET") {
  const response = await fetch(path, {
    method, cache: "no-store", signal: AbortSignal.timeout(method === "GET" ? 10000 : 60000),
  });
  const body = await response.json();
  if (!response.ok) {
    const detail = body.detail ?? body;
    throw new Error(`HTTP ${response.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
  return body;
}

function table(headers, rows) {
  const wrap = element("div", null, "table-wrap");
  const node = document.createElement("table");
  const head = node.createTHead().insertRow();
  for (const label of headers) {
    const cell = element("th", label);
    cell.scope = "col";
    head.append(cell);
  }
  const body = node.createTBody();
  for (const values of rows) {
    const row = body.insertRow();
    for (const value of values) row.append(element("td", value ?? "Unknown"));
  }
  wrap.append(node);
  return wrap;
}

function workloadTable(workloads) {
  return table([
    "Workload", "Ready", "CPU request / limit (m per replica)",
    "CPU avg / p95 (m total)", "Throttled periods (%)",
    "Memory request / limit (MiB per replica)", "Memory peak (MiB total)",
  ], workloads.map(({ spec, metrics }) => [
    spec.name, `${spec.ready_replicas}/${spec.replicas}`,
    `${number(spec.resources.cpu_request_millicores)} / ${number(spec.resources.cpu_limit_millicores)}`,
    `${number(metrics.cpu_avg_millicores)} / ${number(metrics.cpu_p95_millicores)}`,
    number(metrics.cpu_throttling_percent),
    `${number(spec.resources.memory_request_mib)} / ${number(spec.resources.memory_limit_mib)}`,
    number(metrics.memory_peak_mib),
  ]));
}

function allocationTable(item) {
  const spec = item.evidence.workload.spec;
  const before = { ...spec.resources, replicas: spec.replicas };
  const labels = {
    cpu_request_millicores: "CPU request (m)",
    cpu_limit_millicores: "CPU limit (m)",
    memory_request_mib: "Memory request (MiB)",
    memory_limit_mib: "Memory limit (MiB)",
    replicas: "Replicas",
  };
  return table(["Value (resources per replica)", "At analysis", "Proposed"],
    Object.entries(labels).map(([key, label]) => [label, number(before[key]), number(item.new_values[key])]));
}

function pendingCard(item) {
  const card = element("article", null, "card");
  const name = item.evidence.workload.spec.name;
  card.setAttribute("aria-label", `${name} recommendation`);
  card.append(
    element("h3", `${name} - ${item.action} - ${item.risk} risk`),
    element("p", `${item.provider} | ${item.evidence.issues.join(", ")}`, "muted"),
    element("p", item.reason),
    allocationTable(item),
  );
  if (item.monthly_cost_after_usd != null) {
    card.append(element("p", `Estimated monthly cost: ${money(item.monthly_cost_before_usd)} to ${money(item.monthly_cost_after_usd)} (change: ${money(item.monthly_cost_delta_usd)})`));
  }
  const actions = element("div", null, "actions");
  for (const action of ["execute", "dismiss"]) {
    const button = element("button", action === "execute" ? "Execute" : "Dismiss",
      action === "dismiss" ? "secondary" : "");
    button.type = "button";
    button.dataset.action = action;
    button.addEventListener("click", () => performAction(
      `/recommendations/${encodeURIComponent(item.id)}/${action}`, `${name}: ${action}`,
    ));
    actions.append(button);
  }
  card.append(actions);
  return card;
}

function historyCard(item) {
  const card = element("details");
  card.append(element("summary", `${item.evidence.workload.spec.name} - ${item.status} - ${item.created_at}`),
    element("p", item.reason), allocationTable(item));
  if (item.dismissed_reason) card.append(element("p", item.dismissed_reason, "muted"));
  const run = item.execution;
  if (!run) return card;
  if (run.error) card.append(element("p", run.error, "error"));
  if (run.summary) card.append(element("p", run.summary));
  if (run.applied_at) {
    card.append(element("p", `Patch confirmed: ${run.applied_at}`, "muted"));
  } else if (run.patch_attempted_at) {
    card.append(element("p", "Patch attempted but not confirmed. Check the live Deployment before retrying.", "warning"));
  }
  if (run.after_observation) {
    card.append(element("h3", "Before execution"), workloadTable([item.evidence.workload]),
      element("h3", "First complete observation after the metric window"),
      element("p", run.after_observation.observed_at, "muted"),
      workloadTable(run.after_observation.workloads));
  } else if (run.skip_until) {
    card.append(element("p", `Waiting for a clean metric window after ${run.skip_until}.`, "muted"));
  }
  card.append(element("p", `Execution call log: ${run.call_id}`, "muted"));
  return card;
}

function renderList(container, items, render, emptyText) {
  const existing = new Map([...container.children].map((node) => [node.dataset.id, node]));
  if (!items.length) {
    container.replaceChildren(element("p", emptyText, "muted"));
    return;
  }
  const wanted = new Set(items.map((item) => item.id));
  for (const child of [...container.children]) {
    if (!wanted.has(child.dataset.id)) child.remove();
  }
  items.forEach((item, index) => {
    const previous = existing.get(item.id);
    const source = JSON.stringify(item);
    let node = previous;
    // Keep unchanged cards and expanded history intact during polling.
    if (!node || node.dataset.source !== source) {
      node = render(item);
      node.dataset.id = item.id;
      node.dataset.source = source;
      if (previous) {
        if (previous.open) node.open = true;
        previous.replaceWith(node);
      }
    }
    if (container.children[index] !== node) {
      container.insertBefore(node, container.children[index] ?? null);
    }
  });
}

function updateButtons() {
  byId("analyze").disabled = busy || !analysisReadiness
    || !["ready", "no_issues"].includes(analysisReadiness.status);
  for (const button of document.querySelectorAll("[data-action]")) {
    button.disabled = busy || !recommendationsReady;
  }
}

function updateAnalysisReadiness() {
  const node = byId("analysis-readiness");
  if (!analysisReadiness) {
    node.textContent = "Analysis availability is unknown. Waiting for a successful refresh.";
    node.className = "muted";
    return;
  }
  const { status, ready_workloads, waiting_workloads, retry_at, minimum_sized_workloads = [] } = analysisReadiness;
  let message;
  if (status === "waiting_for_metrics") {
    if (retry_at) {
      const seconds = Math.max(0, Math.ceil((Date.parse(retry_at) - Date.now()) / 1000));
      message = seconds > 0
        ? `Collecting fresh post-change metrics. Analysis available in ${Math.floor(seconds / 60)}m ${seconds % 60}s.`
        : "The metric window has elapsed. Waiting for the next fresh observation.";
    } else {
      message = "Changes are queued or executing. Fresh metrics will be collected after they finish.";
    }
  } else if (status === "analyzing") {
    message = "Analysis in progress.";
  } else if (status === "no_issues") {
    message = "No issues detected in the latest observation.";
  } else if (status === "no_changes") {
    message = "No further reduction possible within safety bounds.";
  } else {
    message = `Ready to analyze: ${ready_workloads.join(", ")}.`;
  }
  if (waiting_workloads.length) {
    const skipped = waiting_workloads.map((item) =>
      `${item.workload} (${item.reason === "waiting_for_metrics" ? "collecting fresh metrics" : item.reason})`);
    message += ` Temporarily skipping: ${skipped.join(", ")}.`;
  }
  if (minimum_sized_workloads.length) {
    message += ` Already at minimum CPU, memory and replicas: ${minimum_sized_workloads.join(", ")}.`;
  }
  node.textContent = message;
  node.className = status === "waiting_for_metrics" ? "warning" : "muted";
}

function rememberExecution(item) {
  if (!item.execution) return;
  const previous = executionNotices.get(item.id);
  // Independent GETs can see different moments of the same run; never move
  // a completed run back to executing, or an executing run back to queued.
  if (previous && (["executed", "failed"].includes(previous.status)
      || (previous.status === "executing" && item.status === "queued"))) return;
  executionNotices.set(item.id, item);
}

function updateNotice() {
  const statuses = [...executionNotices.values()].map((item) =>
    `${item.evidence.workload.spec.name}: ${item.status}.`);
  byId("notice").textContent = [actionNotice, ...statuses].filter(Boolean).join("\n");
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  const currentRevision = revision;
  try {
    // Follow accepted IDs even if newer proposals push them out of history.
    const followed = [...executionNotices.values()].filter((item) =>
      ["queued", "executing"].includes(item.status));
    const results = await Promise.allSettled([
      request("/health"), request("/workloads"),
      request("/recommendations?status=pending&limit=1000"),
      request("/recommendations?limit=100"),
      request("/recommendations?status=executing&limit=1"),
      request("/recommendations?status=queued&limit=1000"),
      request("/analysis/readiness"),
      ...followed.map((item) => request(`/recommendations/${encodeURIComponent(item.id)}`)),
    ]);
    // A GET started before a click must not overwrite that action's newer state.
    if (currentRevision !== revision) return;
    const [health, workloads, pending, history, running, queued, readiness] = results;
    byId("health").textContent = health.status === "fulfilled" ? "Healthy" : "Needs attention";
    const labels = [
      "Health", "Workloads (displayed values may be stale)", "Recommendations", "History",
      "Execution status", "Execution queue", "Analysis availability",
      ...followed.map((item) => `${item.evidence.workload.spec.name} execution status`),
    ];
    byId("refresh-error").textContent = results.flatMap((result, index) =>
      result.status === "rejected" ? [`${labels[index]}: ${result.reason.message}`] : []).join("\n");
    if (workloads.status === "fulfilled") {
      const body = workloads.value;
      const costs = body.node_costs;
      byId("node-costs").hidden = !costs;
      byId("node-costs").textContent = costs
        ? `Node compute: $${Number(costs.hourly_usd).toFixed(3)}/hour. ${costs.nodes.map((node) => `${node.name}: ${node.vm_size}, ${node.region}`).join("; ")}.`
        : "";
      byId("cost-note").textContent = costs
        ? "Azure Linux on-demand list prices cover node compute only. Disks, networking and OpenAI calls cost extra. Changing requests alone does not reduce the node bill."
        : "OpenAI analysis and execution can incur API fees. Prices below are requests-based estimates, not actual cloud bills.";
      byId("observed").textContent = `Observed: ${body.observed_at} | window: ${body.window_seconds}s | refresh: 5s`;
      byId("observation-error").textContent = body.error ? `Observation is stale: ${body.error}` : "";
      byId("workloads").replaceChildren(body.workloads.length
        ? workloadTable(body.workloads) : element("p", "No workloads found.", "muted"));
    }
    recommendationsReady = [pending, history, running, queued].every((result) => result.status === "fulfilled");
    analysisReadiness = readiness.status === "fulfilled" ? readiness.value : null;
    updateAnalysisReadiness();
    if (pending.status === "fulfilled") {
      renderList(byId("pending"), pending.value, pendingCard, "No pending recommendations.");
    }
    if (history.status === "fulfilled") {
      renderList(byId("history"), history.value.filter((item) => item.status !== "pending"),
        historyCard, "No history yet.");
      history.value.filter((item) => executionNotices.has(item.id)).forEach(rememberExecution);
    }
    for (const result of [running, queued]) {
      if (result.status === "fulfilled") result.value.forEach(rememberExecution);
    }
    for (const result of results.slice(7)) {
      if (result.status === "fulfilled") rememberExecution(result.value);
    }
    updateNotice();
  } finally {
    refreshing = false;
    updateButtons();
  }
}

async function performAction(path, label) {
  if (busy) return;
  busy = true;
  revision += 1;
  updateButtons();
  byId("action-error").textContent = "";
  actionNotice = `${label}...`;
  updateNotice();
  try {
    const result = await request(path, "POST");
    if (result.execution) {
      rememberExecution(result);
      actionNotice = "";
    } else if (result.status === "waiting_for_metrics") {
      actionNotice = "Waiting for fresh post-change metrics. No analysis call was made.";
    } else if (result.status === "no_changes") {
      actionNotice = result.call_id
        ? "No useful changes were proposed within safety bounds."
        : "No further reduction possible within safety bounds. No analysis call was made.";
    } else {
      actionNotice = result.status === "no_issues" ? "No issues to analyze."
        : `${label}: ${result.status}${result.recommendations ? ` (${result.recommendations.length} recommendations, ${result.invalid_proposals} rejected)` : ""}.`;
    }
    updateNotice();
  } catch (error) {
    actionNotice = "";
    updateNotice();
    byId("action-error").textContent = `${label} failed: ${error.message}. Check current status before retrying.`;
  } finally {
    // The next poll reconciles any result lost through a network error; never retry a POST.
    while (refreshing) await new Promise((resolve) => setTimeout(resolve, 50));
    await refresh();
    busy = false;
    updateButtons();
  }
}

byId("analyze").addEventListener("click", () => performAction("/analyze", "Analyze"));
refresh();
setInterval(refresh, 5000);
// This only updates the label. The server's next observation decides readiness.
setInterval(updateAnalysisReadiness, 1000);
