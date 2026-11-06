// requests-fallback: Cloudflare Worker for Plex Notice
//
// Sits in front of a site (e.g. requests.example.com) via a Workers Route.
// Visitors are passed straight through to your server. If the server can't be
// reached (container down, host down, whole house off), a maintenance page is
// shown instead.
//
// The text on that page comes from Workers KV (bound as NOTICES), written by
// the Plex Notice app:
//   "planned"  set while a notice is active, for planned downtime
//   "default"  your standard "unexpected outage" message
// KV is only read when the server is down, so normal browsing costs nothing extra.
//
// Optional variable FORCE_MAINTENANCE = "true" shows the page even while the
// server is up (Worker → Settings → Variables and Secrets).
//
// Troubleshooting addresses (on your site, e.g. https://requests.example.com/...):
//   /__plex-notice/status   what the Worker can see (KV binding, stored messages)
//   /__plex-notice/preview  the maintenance page as it would look right now

const VERSION = "2.1";

const FALLBACK = {
  title: "Temporarily unavailable",
  message:
    "The server is offline right now, either for planned maintenance or an " +
    "unexpected outage. Everything will be back as soon as possible.",
};

// How long to wait for your server before showing the page (milliseconds)
const TIMEOUT_MS = 10000;

// Status codes that mean "the server isn't reachable", not "the app had an error"
const DOWN_STATUSES = new Set([502, 504, 520, 521, 522, 523, 524, 525, 526, 530]);

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path === "/__plex-notice/status") return statusReport(env);
    if (path === "/__plex-notice/preview") return maintenancePage(await loadNotice(env));

    if (env.FORCE_MAINTENANCE === "true") {
      return maintenancePage(await loadNotice(env));
    }

    try {
      const timeout = new Promise((_, reject) =>
        setTimeout(() => reject(new Error("timeout")), TIMEOUT_MS)
      );
      const response = await Promise.race([fetch(request), timeout]);
      if (DOWN_STATUSES.has(response.status)) {
        return maintenancePage(await loadNotice(env));
      }
      return response;
    } catch (err) {
      return maintenancePage(await loadNotice(env));
    }
  },
};

async function loadNotice(env) {
  if (!env.NOTICES) {
    return { ...FALLBACK, planned: false, source: "built-in text (no KV binding called NOTICES)" };
  }
  try {
    const now = Math.floor(Date.now() / 1000);
    const planned = await env.NOTICES.get("planned", { type: "json" });
    if (planned && (planned.message || planned.title) && (!planned.expires || planned.expires > now)) {
      return {
        title: planned.title || FALLBACK.title,
        message: planned.message || FALLBACK.message,
        until: planned.until_display || "",
        planned: true,
        source: "KV planned",
      };
    }
    const def = await env.NOTICES.get("default", { type: "json" });
    if (def && (def.message || def.title)) {
      return { title: def.title || FALLBACK.title, message: def.message || FALLBACK.message,
               planned: false, source: planned ? "KV default (planned has expired)" : "KV default" };
    }
    return { ...FALLBACK, planned: false, source: "built-in text (KV has no planned or default message)" };
  } catch (e) {
    return { ...FALLBACK, planned: false, source: `built-in text (KV error: ${e.message})` };
  }
}

async function statusReport(env) {
  const report = {
    worker_version: VERSION,
    now: Math.floor(Date.now() / 1000),
    force_maintenance: env.FORCE_MAINTENANCE === "true",
    kv_binding_NOTICES: Boolean(env.NOTICES),
  };
  if (env.NOTICES) {
    try {
      report.planned = await env.NOTICES.get("planned", { type: "json" });
      report.default = await env.NOTICES.get("default", { type: "json" });
    } catch (e) {
      report.kv_error = e.message;
    }
  }
  report.page_would_show = (await loadNotice(env)).source;
  return new Response(JSON.stringify(report, null, 2), {
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function maintenancePage(n) {
  const tag = n.planned ? "Planned maintenance" : "Server offline";
  const tagColour = n.planned ? "#f5b041" : "#ff6b6b";
  const tagBg = n.planned ? "#3a2a10" : "#3a1416";
  const message = esc(n.message).replace(/\n/g, "<br>");
  const until = n.until ? `<p class="until">Expected back: <strong>${esc(n.until)}</strong></p>` : "";

  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(n.title)}</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #0d0f12; color: #e6e6e6;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .card {
    max-width: 520px; margin: 24px; padding: 32px;
    background: #16191e; border: 1px solid #2a2f37; border-radius: 14px;
  }
  .tag {
    display: inline-block; padding: 4px 10px; border-radius: 999px;
    background: ${tagBg}; color: ${tagColour}; font-size: 13px; font-weight: 600;
    letter-spacing: .04em; text-transform: uppercase;
  }
  h1 { font-size: 24px; margin: 16px 0 8px; }
  p { line-height: 1.55; color: #b8bec7; margin: 0 0 12px; }
  .until { color: #e6e6e6; }
  small { color: #7d8590; }
</style>
</head>
<body>
  <!-- plex-notice worker ${VERSION}: ${esc(n.source || "")} -->
  <div class="card">
    <span class="tag">${tag}</span>
    <h1>${esc(n.title)}</h1>
    <p>${message}</p>
    ${until}
    <small>This page checks again every time you reload it.</small>
  </div>
</body>
</html>`;

  return new Response(html, {
    status: 503,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "retry-after": "300",
      "x-plex-notice-source": n.source || "",
    },
  });
}
