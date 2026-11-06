// Shared helpers for every page.
document.addEventListener("submit", (e) => {
  const msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const el = document.querySelector(btn.getAttribute("data-copy"));
  if (!el) return;
  try {
    await navigator.clipboard.writeText(el.innerText);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = el.innerText;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  const old = btn.textContent;
  btn.textContent = "Copied!";
  setTimeout(() => (btn.textContent = old), 1500);
});

function fmtTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit",
  });
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
