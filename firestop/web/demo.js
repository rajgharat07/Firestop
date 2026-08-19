"use strict";

const CAPTURES = {
  health: "health.json",
  overview: "overview.json",
  blast: "blast.json",
  fix: "fix.json",
  pivot: "pivot.json",
};

const LODASH_ONLY =
  "This hosted snapshot only has the lodash <4.17.21 incident. Clone the repo to query live HydraDB.";

export function demoEnabled() {
  return Boolean(typeof window !== "undefined" && window.__FIRESTOP_DEMO__);
}

export async function demoResolve(path, params = new URLSearchParams()) {
  const endpoint = path.replace(/\/+$/, "").split("/").pop();
  const file = CAPTURES[endpoint];
  if (!file) {
    throw new Error(`this snapshot has no capture for ${path}`);
  }

  const pkg = (params.get && params.get("package")) || "";
  const advisory = (params.get && params.get("advisory")) || "";

  if (endpoint === "blast" || endpoint === "fix") {
    if (advisory || (pkg && pkg !== "lodash")) {
      throw new Error(LODASH_ONLY);
    }
  }
  if (endpoint === "pivot" && pkg && pkg !== "lodash") {
    throw new Error("This hosted snapshot only has the lodash maintainer pivot.");
  }

  const url = new URL(`./demo/${file}`, import.meta.url);
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`demo capture missing (${response.status})`);
  }
  return response.json();
}
