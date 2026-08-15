"""A token-protected page showing real API activity — who called what, when.

Two sources, merged into one timeline:

- **The app log**, read from the in-memory `RingBufferHandler` installed by
  `logging_setup.configure` — every webhook received, every eko API call,
  every agent tool call, every WhatsApp send already logs a line; this just
  makes the recent ones visible without shell access.
- **The payment audit trail** (`AuditTrail`), the only source with structured
  `payer` / `payee` / `amount_paise` fields — "who is initiating payment" is
  answered from here, not guessed out of a log message.

Never mounted unless `OPS_ENABLED=true`, and every route refuses without
`OPS_TOKEN` matching — unlike `/demo`, this is real traffic, not stub data.
"""

from __future__ import annotations

import hmac
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import HTMLResponse

from . import logging_setup
from .config import Settings


def _require_token(settings: Settings, x_ops_token: str | None) -> None:
    if not settings.ops.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not settings.ops.token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPS_TOKEN is not set — refusing to serve real activity data unauthenticated.",
        )
    if not x_ops_token or not hmac.compare_digest(x_ops_token, settings.ops.token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or wrong X-Ops-Token."
        )


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    """The last `limit` parseable lines of a JSONL file. Never raises — a torn
    final line (a crash mid-write) is expected on an append-only file."""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _payment_detail(record: dict[str, Any]) -> str:
    parts = [
        f"payer={record.get('payer')}",
        f"payee={record.get('payee')}",
    ]
    if record.get("payee_name"):
        parts.append(f"name={record['payee_name']}")
    if record.get("amount_paise") is not None:
        parts.append(f"amount_paise={record['amount_paise']}")
    if record.get("status"):
        parts.append(f"status={record['status']}")
    if record.get("reason"):
        parts.append(f"reason={record['reason']}")
    if record.get("reference"):
        parts.append(f"ref={record['reference']}")
    return " ".join(parts)


def create_ops_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/ops", tags=["ops"])

    @router.get("/activity")
    def activity(x_ops_token: str | None = Header(default=None)) -> dict[str, Any]:
        _require_token(settings, x_ops_token)
        limit = settings.ops.tail_limit

        events: list[dict[str, Any]] = []

        buffer = logging_setup.get_ring_buffer()
        if buffer is not None:
            for rec in buffer.snapshot()[-limit:]:
                events.append({
                    "ts": rec["time"],
                    "source": "app",
                    "kind": rec["level"],
                    "actor": None,
                    "detail": f"[{rec['logger']}] {rec['message']}",
                })

        audit_path = settings.payments.audit_path
        if audit_path:
            for rec in _tail_jsonl(Path(audit_path), limit):
                events.append({
                    "ts": rec.get("ts"),
                    "source": "payment",
                    "kind": rec.get("event", "payment"),
                    "actor": rec.get("payer"),
                    "detail": _payment_detail(rec),
                })

        # Newest first. Missing timestamps sort last rather than raising.
        events.sort(key=lambda e: e.get("ts") or "", reverse=True)
        return {"events": events[:limit], "server_time": events[0]["ts"] if events else None}

    @router.get("", response_class=HTMLResponse)
    def page() -> str:
        # 404s here too when disabled, same as the API route — no page for a
        # feature that isn't on, and no hint via a 401 that it merely needs a
        # token.
        if not settings.ops.enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return _PAGE_HTML

    return router


_PAGE_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Activity — WhatsApp Voice Agent</title>
<style>
  :root {
    --bg: #0b0e14; --panel: #131720; --border: #232838; --text: #e6e9ef;
    --dim: #8a92a6; --accent: #4f8cff; --ok: #2fbf71; --warn: #e0a72e; --err: #e5484d;
    --header-h: 49px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  header {
    display: flex; align-items: center; justify-content: space-between;
    height: var(--header-h); padding: 0 20px; border-bottom: 1px solid var(--border);
    position: sticky; top: 0; background: var(--bg); z-index: 2;
  }
  h1 { font-size: 15px; font-weight: 600; margin: 0; }
  #status { color: var(--dim); font-size: 12px; }
  main { padding: 12px 20px 40px; max-width: 100%; overflow-x: auto; }
  /* separate, not collapse: Chromium/WebKit mis-paint a sticky <th> inside a
     border-collapse table — the ghosting/overlap this caused is exactly why
     sticky column headers were dropped below, not just this line. */
  table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 12.5px; }
  th, td {
    text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  td.detail { white-space: pre-wrap; word-break: break-word; }
  /* Not sticky. A sticky <th> here rendered as overlapping/ghosted text on
     top of the row beneath it (a real Chromium/WebKit bug with sticky table
     cells) — the outer <header> staying pinned is enough; column labels
     scrolling with the table is the trade-off for a table that renders
     correctly instead of nearly correctly. */
  th { color: var(--dim); font-weight: 500; background: var(--bg); }
  .badge {
    display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px;
    font-weight: 600;
  }
  .src-app { background: #1c2333; color: var(--accent); }
  .src-payment { background: #23301f; color: var(--ok); }
  .lvl-WARNING { color: var(--warn); }
  .lvl-ERROR, .lvl-CRITICAL { color: var(--err); }
  #login {
    display: flex; flex-direction: column; gap: 10px; max-width: 360px;
    margin: 80px auto; padding: 24px; background: var(--panel);
    border: 1px solid var(--border); border-radius: 10px;
  }
  #login input {
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    padding: 8px 10px; border-radius: 6px; font: inherit;
  }
  #login button, #logout {
    background: var(--accent); color: white; border: none; padding: 8px 12px;
    border-radius: 6px; cursor: pointer; font: inherit; font-weight: 600;
  }
  #logout { background: transparent; color: var(--dim); border: 1px solid var(--border); }
  #error { color: var(--err); font-size: 12.5px; min-height: 1em; }
  .hidden { display: none !important; }
</style>
</head>
<body>

<div id="login">
  <h1>Activity log</h1>
  <p style="color: var(--dim); margin: 0;">
    Real API traffic — WhatsApp, eko, the agent. Token required.
  </p>
  <input id="token-input" type="password" placeholder="OPS_TOKEN" autocomplete="off">
  <button id="token-submit">View activity</button>
  <div id="error"></div>
</div>

<div id="app" class="hidden">
  <header>
    <h1>Activity — WhatsApp Voice Agent</h1>
    <div style="display:flex; align-items:center; gap: 14px;">
      <span id="status">connecting…</span>
      <button id="logout">forget token</button>
    </div>
  </header>
  <main>
    <table>
      <thead>
        <tr><th>time (UTC)</th><th>source</th><th>kind</th><th>actor</th><th>detail</th></tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </main>
</div>

<script>
const STORAGE_KEY = "wa_voxo_ops_token";
const loginEl = document.getElementById("login");
const appEl = document.getElementById("app");
const errorEl = document.getElementById("error");
const statusEl = document.getElementById("status");
const rowsEl = document.getElementById("rows");
let timer = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

async function fetchActivity(token) {
  const res = await fetch("/ops/activity", { headers: { "X-Ops-Token": token } });
  if (res.status === 401) throw new Error("unauthorized");
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

function render(events) {
  rowsEl.innerHTML = events.map(e => `
    <tr>
      <td>${esc((e.ts || "").replace("T", " ").replace("Z", ""))}</td>
      <td><span class="badge src-${esc(e.source)}">${esc(e.source)}</span></td>
      <td class="lvl-${esc(e.kind)}">${esc(e.kind)}</td>
      <td>${esc(e.actor || "")}</td>
      <td class="detail">${esc(e.detail)}</td>
    </tr>
  `).join("");
}

async function tick(token) {
  try {
    const data = await fetchActivity(token);
    render(data.events);
    statusEl.textContent =
      "updated " + new Date().toLocaleTimeString() + " · " + data.events.length + " events";
  } catch (err) {
    if (err.message === "unauthorized") {
      stop();
      showLogin("Wrong token.");
      return;
    }
    statusEl.textContent = "error: " + err.message;
  }
}

function start(token) {
  loginEl.classList.add("hidden");
  appEl.classList.remove("hidden");
  tick(token);
  timer = setInterval(() => tick(token), 3000);
}

function stop() {
  if (timer) clearInterval(timer);
  timer = null;
  localStorage.removeItem(STORAGE_KEY);
}

function showLogin(message) {
  appEl.classList.add("hidden");
  loginEl.classList.remove("hidden");
  errorEl.textContent = message || "";
}

document.getElementById("token-submit").addEventListener("click", () => {
  const token = document.getElementById("token-input").value.trim();
  if (!token) return;
  localStorage.setItem(STORAGE_KEY, token);
  start(token);
});
document.getElementById("token-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("token-submit").click();
});
document.getElementById("logout").addEventListener("click", () => {
  stop();
  showLogin();
});

const saved = localStorage.getItem(STORAGE_KEY);
if (saved) start(saved); else showLogin();
</script>
</body>
</html>
"""
