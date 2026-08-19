"use strict";

import { demoEnabled, demoResolve } from "./demo.js";

const NAV_LINKS = [
  { href: "/", label: "Home", match: /^\/$/ },
  { href: "/console", label: "Console", match: /^\/console\/?$/ },
  { href: "/how-it-works", label: "How it works", match: /^\/how-it-works\/?$/ },
  { href: "/features", label: "Features", match: /^\/features\/?$/ },
  { href: "/architecture", label: "Architecture", match: /^\/architecture\/?$/ },
  { href: "/docs", label: "API", match: /^\/docs/, external: true },
];

const REPO_URL = "https://github.com/rajgharat07/Firestop";

function navItems() {
  if (!demoEnabled()) return NAV_LINKS;
  return NAV_LINKS.map((link) =>
    link.href === "/docs"
      ? { href: REPO_URL, label: "GitHub", match: null, external: true }
      : link,
  );
}

const reduceMotion = () =>
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function activeHref() {
  const path = location.pathname.replace(/\/$/, "") || "/";
  return path;
}

function renderNav() {
  const path = activeHref();
  const links = navItems()
    .map((link) => {
      const hrefPath = link.href.replace(/\/$/, "") || "/";
      const isActive = link.match
        ? link.match.test(location.pathname)
        : !link.external && hrefPath === path;
      const classes = [
        isActive ? "active" : "",
        link.href === "/console" ? "nav-cta" : "",
      ]
        .filter(Boolean)
        .join(" ");
      const extra = link.external ? ' target="_blank" rel="noreferrer"' : "";
      return `<a href="${link.href}" class="${classes}"${
        isActive ? ' aria-current="page"' : ""
      }${extra}>${link.label}</a>`;
    })
    .join("");

  return `
    <nav class="site-nav" aria-label="Primary">
      <a class="nav-brand" href="/">
        <img src="/static/mark.svg" alt="" width="28" height="28" />
        <strong>Firestop</strong>
      </a>
      <button type="button" class="nav-toggle" aria-expanded="false" aria-controls="nav-links">
        Menu
      </button>
      <div class="nav-links" id="nav-links">${links}</div>
    </nav>`;
}

function renderFooter() {
  const apiHref = demoEnabled() ? REPO_URL : "/docs";
  const apiLabel = demoEnabled() ? "GitHub" : "API";
  return `
    <footer class="site-footer">
      <span>${
        demoEnabled()
          ? "Hosted snapshot of the lodash incident. Clone the repo to run against live HydraDB."
          : "Firestop runs entirely on HydraDB. No model calls, no API keys."
      }</span>
      <div class="footer-links">
        <a href="/console">Console</a>
        <a href="${apiHref}">${apiLabel}</a>
        <a href="/architecture">Architecture</a>
      </div>
    </footer>`;
}

function mountShell() {
  document.body.classList.add("has-embers");

  const navHost = document.getElementById("site-nav");
  if (navHost) navHost.outerHTML = renderNav();
  else document.body.insertAdjacentHTML("afterbegin", renderNav());

  const footerHost = document.getElementById("site-footer");
  if (footerHost) footerHost.outerHTML = renderFooter();
  else document.body.insertAdjacentHTML("beforeend", renderFooter());

  const toggle = document.querySelector(".nav-toggle");
  const links = document.getElementById("nav-links");
  if (toggle && links) {
    toggle.addEventListener("click", () => {
      const open = links.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
}

function mountEmbers() {
  if (reduceMotion()) return;
  if (document.getElementById("ember-canvas")) return;

  const canvas = document.createElement("canvas");
  canvas.id = "ember-canvas";
  canvas.setAttribute("aria-hidden", "true");
  document.body.prepend(canvas);

  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  let width = 0;
  let height = 0;
  let raf = 0;
  const particles = [];

  function resize() {
    width = canvas.width = window.innerWidth;
    height = canvas.height = window.innerHeight;
  }

  function spawn(count) {
    for (let i = 0; i < count; i += 1) {
      particles.push({
        x: Math.random() * width,
        y: height + Math.random() * 80,
        r: 0.6 + Math.random() * 2.2,
        vy: 0.25 + Math.random() * 0.9,
        vx: (Math.random() - 0.5) * 0.35,
        a: 0.15 + Math.random() * 0.45,
      });
    }
  }

  function frame() {
    ctx.clearRect(0, 0, width, height);
    for (const p of particles) {
      p.y -= p.vy;
      p.x += p.vx + Math.sin(p.y * 0.01) * 0.15;
      if (p.y < -10) {
        p.y = height + 10;
        p.x = Math.random() * width;
      }
      ctx.beginPath();
      ctx.fillStyle = `rgba(255, 106, 42, ${p.a})`;
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    }
    raf = requestAnimationFrame(frame);
  }

  resize();
  spawn(Math.min(64, Math.floor(width / 24)));
  window.addEventListener("resize", () => {
    resize();
    if (particles.length < 40) spawn(20);
  });
  raf = requestAnimationFrame(frame);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) cancelAnimationFrame(raf);
    else raf = requestAnimationFrame(frame);
  });
}

function mountReveal() {
  const nodes = document.querySelectorAll(".reveal");
  if (!nodes.length) return;

  if (reduceMotion() || !("IntersectionObserver" in window)) {
    nodes.forEach((node) => node.classList.add("in"));
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          observer.unobserve(entry.target);
        }
      }
    },
    { threshold: 0.12, rootMargin: "0px 0px -40px 0px" }
  );

  nodes.forEach((node) => observer.observe(node));
}

export function countUp(el, target, { duration = 900, decimals = 0 } = {}) {
  if (!el) return;
  const end = Number(target);
  if (!Number.isFinite(end)) {
    el.textContent = String(target);
    return;
  }
  if (reduceMotion()) {
    el.textContent = end.toLocaleString(undefined, {
      maximumFractionDigits: decimals,
    });
    return;
  }

  const start = performance.now();
  function tick(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    const value = end * eased;
    el.textContent = value.toLocaleString(undefined, {
      maximumFractionDigits: decimals,
      minimumFractionDigits: decimals,
    });
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

export async function fetchJson(path, params = new URLSearchParams()) {
  if (demoEnabled()) return demoResolve(path, params);

  const query = params.toString();
  const url = query ? `${path}?${query}` : path;
  const response = await fetch(url);
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

export function hydraHint(error) {
  const text = String(error.message || error);
  if (/connection attempts failed|HydraUnavailable|Failed to fetch/i.test(text)) {
    return (
      "HydraDB is not reachable. Start Docker, then " +
      "`docker compose up -d` and `python -m firestop doctor`."
    );
  }
  return text;
}

function enhanceNavTransitions() {
  if (!document.startViewTransition || reduceMotion()) return;

  document.addEventListener("click", (event) => {
    const anchor = event.target.closest("a");
    if (!anchor) return;
    const href = anchor.getAttribute("href");
    if (!href || href.startsWith("#") || href.startsWith("http") || href === "/docs") {
      return;
    }
    if (anchor.target === "_blank" || event.metaKey || event.ctrlKey) return;
    const url = new URL(href, location.href);
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname) return;

    event.preventDefault();
    document.startViewTransition(() => {
      location.href = url.href;
    });
  });
}

export async function loadGraphStats(targets = {}) {
  const health = await fetchJson("/api/health");
  const counts = health.vertices || {};
  const mapping = {
    Package: targets.packages,
    Release: targets.releases,
    Advisory: targets.advisories,
    Service: targets.services,
  };
  for (const [key, el] of Object.entries(mapping)) {
    if (!el) continue;
    const value = counts[key];
    if (value === undefined || value === null) {
      el.textContent = "—";
    } else {
      countUp(el, value);
    }
  }
  return { health, counts };
}

function boot() {
  mountShell();
  mountEmbers();
  mountReveal();
  enhanceNavTransitions();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
