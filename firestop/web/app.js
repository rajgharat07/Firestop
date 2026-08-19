"use strict";

import { demoEnabled, demoResolve } from "./demo.js";

const el = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat();

const SCRUB_START = Date.UTC(2020, 0, 1) / 1000;
let scrubEnd = Math.floor(Date.now() / 1000);

const state = { advisories: [], running: false };

function asOfSeconds() {
  const position = Number(el("asof").value);
  if (position >= 100) return null;
  return Math.round(SCRUB_START + ((scrubEnd - SCRUB_START) * position) / 100);
}

function asOfLabel() {
  const moment = asOfSeconds();
  if (moment === null) return "now";
  return new Date(moment * 1000).toISOString().slice(0, 10);
}

function paintRail() {
  el("asof-label").textContent = asOfLabel();
}

function incident() {
  const params = new URLSearchParams();
  const advisory = el("advisory").value.trim();
  const pkg = el("package").value.trim();

  if (advisory) params.set("advisory", advisory);
  else if (pkg) {
    params.set("package", pkg);
    params.set("versions", el("versions").value.trim() || "*");
  } else return null;

  const moment = asOfSeconds();
  if (moment !== null) params.set("as_of", String(moment));
  params.set("build", el("build").checked ? "true" : "false");
  return params;
}

async function fetchJson(path, params) {
  if (demoEnabled()) return demoResolve(path, params);

  const response = await fetch(`${path}?${params}`);
  let body;
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  if (!response.ok) {
    const detail = body.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((item) => item.msg || item).join("; ")
          : `request failed (${response.status})`;
    throw new Error(message);
  }
  return body;
}

function say(message, kind = "") {
  const status = el("status");
  status.textContent = message;
  status.classList.toggle("bad", kind === "bad");
  status.classList.toggle("live", kind === "live");
}

function showResults() {
  const summary = el("summary");
  if (summary) summary.hidden = true;
}

function showEmpty() {
  const summary = el("summary");
  if (!summary) return;
  summary.hidden = false;
  summary.innerHTML = `
    <p class="empty">
      Name a compromised package or advisory, then compute. Paths and
      remediation appear here.
    </p>`;
}

function hydraHint(error) {
  const text = String(error.message || error);
  if (/connection attempts failed|HydraUnavailable|Failed to fetch/i.test(text)) {
    return (
      "HydraDB is not reachable. Start Docker, then " +
      "`docker compose up -d` and `python -m firestop doctor`."
    );
  }
  return text;
}

function showBanner(message, kind = "error") {
  const banner = el("hydra-banner");
  if (!banner) return;
  banner.hidden = !message;
  if (!message) {
    banner.innerHTML = "";
    return;
  }
  banner.className = `banner ${kind}`;
  banner.style.margin = "12px 24px 0";
  banner.innerHTML = message;
  banner.hidden = false;
}

function skeletonCard(lines = 3) {
  const rows = Array.from({ length: lines }, (_, index) => {
    const width = index === 0 ? "lg" : index === lines - 1 ? "sm" : "";
    return `<div class="skeleton skeleton-line ${width}"></div>`;
  }).join("");
  return `<div class="card"><div class="skeleton skeleton-block"></div>${rows}</div>`;
}

function paintLoading() {
  showResults();
  el("stage-blast").innerHTML = skeletonCard(3);
  el("stage-paths").innerHTML = skeletonCard(2);
  el("stage-seal").innerHTML = `
    <div class="card seal">
      <h3>Seal</h3>
      <div class="skeleton skeleton-line lg"></div>
      <div class="skeleton skeleton-line"></div>
      <div class="skeleton skeleton-line sm"></div>
    </div>`;
  el("stage-pivot").innerHTML = "";
}

async function loadOverview() {
  try {
    const data = await fetchJson("/api/overview", new URLSearchParams());
    state.advisories = data.advisories || [];

    el("advisories").innerHTML = state.advisories
      .map((row) => `<option value="${escape(row.osv_id)}">${escape(row.summary || "")}</option>`)
      .join("");
    el("packages").innerHTML = (data.packages || [])
      .map((row) => `<option value="${escape(row.name)}"></option>`)
      .join("");

    const health = await fetchJson("/api/health", new URLSearchParams());
    const counts = health.vertices || {};
    const stats = [
      ["Packages", counts.Package],
      ["Releases", counts.Release],
      ["Advisories", counts.Advisory],
      ["Services", counts.Service],
    ].filter(([, value]) => value !== undefined && value !== null);

    if (!stats.length) {
      el("graph-stats").innerHTML =
        "<div><b>0</b>Graph empty — crawl, then load lockfiles</div>";
    } else {
      el("graph-stats").innerHTML = stats
        .map(([name, value]) => `<div><b>${fmt.format(value)}</b>${name}</div>`)
        .join("");
    }

    el("asof-start").textContent = new Date(SCRUB_START * 1000).toISOString().slice(0, 10);
    paintRail();
    showBanner("");
  } catch (error) {
    const hint = hydraHint(error);
    say(hint, "bad");
    showBanner(hint, "error");
  }
}

async function run() {
  const params = incident();
  if (!params) {
    showEmpty();
    el("stage-blast").innerHTML = "";
    el("stage-paths").innerHTML = "";
    el("stage-seal").innerHTML = "";
    el("stage-pivot").innerHTML = "";
    return say("Name an advisory or a package first.", "bad");
  }
  if (state.running) return;

  state.running = true;
  el("run").disabled = true;
  el("compose").classList.add("live");
  paintLoading();
  say("one algo.MSpaths call — every service against every affected release…", "live");

  try {
    const radius = await fetchJson("/api/blast", params);
    showResults();
    renderBlast(radius);
    renderPaths(radius);
    say(
      `${fmt.format(radius.paths_returned)} paths returned, ` +
        `${fmt.format(radius.paths_live)} live, in ${radius.elapsed_ms} ms`
    );

    if (radius.exposed > 0) {
      el("stage-seal").innerHTML = `
        <div class="card seal">
          <h3>Seal</h3>
          <div class="skeleton skeleton-line lg"></div>
          <div class="skeleton skeleton-line"></div>
          <p class="hint">planning the fewest bumps…</p>
        </div>`;
      const plan = await fetchJson("/api/fix", params);
      renderSeal(plan);
      await renderPivot(params);
    } else {
      el("stage-seal").innerHTML = "";
      el("stage-pivot").innerHTML = "";
    }
    showBanner("");
  } catch (error) {
    const hint = hydraHint(error);
    say(hint, "bad");
    showBanner(hint, "error");
    el("stage-blast").innerHTML = `
      <div class="card">
        <h3>Could not compute</h3>
        <p class="hint">${escape(hint)}</p>
      </div>`;
    el("stage-paths").innerHTML = "";
    el("stage-seal").innerHTML = "";
    el("stage-pivot").innerHTML = "";
  } finally {
    state.running = false;
    el("run").disabled = false;
    el("compose").classList.remove("live");
  }
}

function renderBlast(radius) {
  const found = radius.compromise;
  const label = found.advisory || found.summary || found.packages.join(", ");
  const when = radius.as_of
    ? new Date(radius.as_of * 1000).toISOString().slice(0, 10)
    : "now";
  const tallyClass = radius.exposed ? "hot" : "cool";

  const rows = radius.services
    .map(
      (service) => `
      <tr>
        <td class="${service.criticality === "tier-1" ? "tier-1" : ""}">${escape(service.service)}</td>
        <td class="mono">${escape(service.criticality)}</td>
        <td class="mono">${service.shortest}</td>
        <td class="mono">${service.paths.length}</td>
        <td>
          <span class="badge ${service.runtime ? "runtime" : ""}">${
            service.runtime ? "runtime" : "build only"
          }</span>
          ${service.direct ? '<span class="badge">declared</span>' : ""}
        </td>
      </tr>`
    )
    .join("");

  el("stage-blast").innerHTML = `
    <div class="card">
      <div class="headline">
        <h3>${escape(label)}</h3>
        ${found.severity ? `<span class="pill ${escape(found.severity)}">${escape(found.severity)}</span>` : ""}
        <span class="when">as of ${when}</span>
      </div>
      <p class="sub">${fmt.format(found.releases.length)} affected release(s) of
        ${escape(found.packages.join(", "))}${
          found.fixed_in.length ? ` · fixed in ${escape(found.fixed_in.join(", "))}` : ""
        }</p>
      <div class="numbers ${tallyClass}">
        <div class="${radius.exposed ? "hot" : "cool"}">
          <b>${radius.exposed}</b><span>services exposed</span>
        </div>
        <div><b>${fmt.format(radius.paths_live)}</b><span>live paths</span></div>
        <div><b>${fmt.format(radius.elapsed_ms)}</b><span>ms</span></div>
      </div>
      ${
        radius.truncated
          ? `<p class="hint">The traversal hit its path ceiling, so this understates the radius.</p>`
          : ""
      }
      ${
        radius.shortened
          ? `<p class="hint">Answered within ${radius.depth} hops rather than the full ask.</p>`
          : ""
      }
    </div>
    ${
      rows
        ? `<div class="card">
      <h3>Exposed services</h3>
      <table>
        <thead><tr><th>service</th><th>tier</th><th>hops</th><th>paths</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`
        : ""
    }`;
}

function nodeRole(step, index, last) {
  if (index === 0) return "start";
  if (index === last) return "end";
  if (/lock\.(json|yaml)|yarn\.lock|pnpm-lock/i.test(step)) return "pin";
  return "";
}

function renderPaths(radius) {
  const chains = radius.services
    .slice(0, 4)
    .flatMap((service) =>
      service.paths.slice(0, 3).map((path) => ({ service: service.service, path }))
    );

  if (!chains.length) {
    el("stage-paths").innerHTML = radius.exposed
      ? ""
      : `<div class="card">
          <h3>How it gets in</h3>
          <p class="hint">No service reaches it at this moment. Scrub the as-of slider or widen the compromise set.</p>
        </div>`;
    return;
  }

  el("stage-paths").innerHTML = `
    <div class="card penetrations">
      <h3>How it gets in</h3>
      ${chains
        .map(({ path }) => {
          const last = path.chain.length - 1;
          const nodes = path.chain
            .map((step, index) => {
              const role = nodeRole(step, index, last);
              return `<span class="node ${role}">${escape(step)}</span>`;
            })
            .join('<span class="arrow">&rsaquo;</span>');
          return `<div class="chain">${nodes}</div>`;
        })
        .join("")}
      <p class="hint">Each chain crosses lockfile, pin and dependency edges in one traversal.</p>
    </div>`;
}

function renderSeal(plan) {
  if (!plan.changes.length) {
    el("stage-seal").innerHTML = "";
    return;
  }

  const cuts = plan.changes
    .map(
      (change) => `
      <div class="cut">
        <div>${escape(change.description)}</div>
        <div class="to ${change.blocked ? "blocked" : ""}">${
          change.blocked ? "nothing clean" : escape(change.to_version)
        }</div>
        <div class="owner ${change.mine ? "ours" : ""}">${change.mine ? "ours" : "upstream"}</div>
      </div>`
    )
    .join("");

  el("stage-seal").innerHTML = `
    <div class="card seal">
      <h3>Seal</h3>
      ${cuts}
      <p class="hint">
        ${plan.changes.length} change(s) cut ${plan.severed} of ${plan.paths} live paths.
        Bumping every service separately would take ${plan.naive}.
        ${plan.followable ? "Every change has a version to move to." : "Some hops have nowhere clean to go."}
      </p>
    </div>`;
}

async function renderPivot(params) {
  const pkg = params.get("package");
  if (!pkg) {
    el("stage-pivot").innerHTML = "";
    return;
  }

  try {
    const found = await fetchJson("/api/pivot", new URLSearchParams({ package: pkg, limit: "8" }));
    if (!found.maintainers.length) {
      el("stage-pivot").innerHTML = "";
      return;
    }

    const names = (found.siblings || [])
      .slice(0, 8)
      .map((sibling) => `<li>${escape(sibling.package)}</li>`)
      .join("");

    el("stage-pivot").innerHTML = `
      <div class="card publishers">
        <h3>Same publishers</h3>
        <p class="sub">${escape(found.maintainers.join(", "))} can also ship
          ${fmt.format(found.reach)} other package(s) in this graph.</p>
        ${names ? `<ul>${names}</ul>` : ""}
      </div>`;
  } catch {
    el("stage-pivot").innerHTML = "";
  }
}

function escape(text) {
  return String(text).replace(/[&<>"]/g, (character) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[character]
  );
}

el("asof").addEventListener("input", paintRail);
el("asof").addEventListener("change", () => {
  if (el("advisory").value.trim() || el("package").value.trim()) run();
});
el("run").addEventListener("click", run);
document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.tagName === "INPUT") run();
});

paintRail();
loadOverview().then(() => {
  if (demoEnabled()) run();
});
