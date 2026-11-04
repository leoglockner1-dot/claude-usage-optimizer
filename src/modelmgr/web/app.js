"use strict";

async function api(path) {
  const r = await fetch(`/api/${path}`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

(async () => {
  try {
    const [status, overview] = await Promise.all([api("status"), api("overview?days=30")]);
    document.getElementById("status").textContent =
      `mode ${status.mode}${status.disabled ? " (kill switch on)" : ""} · policy v${status.policy_version ?? "—"}`;
    document.getElementById("overview").textContent = JSON.stringify(overview, null, 2);
  } catch (err) {
    document.getElementById("status").textContent = `Could not load data: ${err.message}`;
  }
})();
