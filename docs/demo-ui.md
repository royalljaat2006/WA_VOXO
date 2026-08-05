# Demo UI

A React app for showing the system to people, plus the demo-only HTTP API it
talks to.

**The point of it:** the real path needs Meta credentials, a public HTTPS URL and
a phone. That is three blockers between "the code works" and "someone can watch it
work". This runs the *same* transcriber and the *same* agent over a plain HTTP
call, so you can demo today.

It also shows something WhatsApp cannot: **where the seconds actually go**.

---

## This is not a change of architecture

[ARCHITECTURE.md](ARCHITECTURE.md) says the system has no frontend, and that is
still true. WhatsApp is the product's interface. This is a **presentation tool** —
it is not shipped, not deployed with the product, and nothing in `src/` depends on
it. If it were deleted tomorrow the system would be unaffected.

That is why it lives in `frontend/` with its own dependencies, per
[ARCHITECTURE.md §7](ARCHITECTURE.md#7-if-a-frontend-is-ever-added).

---

## Run it

**Two terminals.**

Backend:
```bash
DEMO_ENABLED=true AUTH_ENABLED=false HANDLER=agent \
  .venv/bin/python tools/run_server.py
```

Frontend:
```bash
npm install --prefix frontend      # first time only
npm run dev --prefix frontend
```

Open <http://localhost:5173>.

`AUTH_ENABLED=false` is only for local use — the demo API has no phone number to
check against the allowlist, so the guard would deny everything.

For a transcript-only demo (no Anthropic key needed), use `HANDLER=transcribe`.
Recording and transcription still work; the reply is the transcript.

---

## What the screen shows

| Area | Shows |
|---|---|
| **Header chips** | The live backend config — handler, speech engine, model. Read from `/demo/status`, not hardcoded |
| **Amber warning** | Appears whenever campaign data is the stub. Every figure below it is invented |
| **Chat column** | WhatsApp-styled. Hold the mic and speak, or type |
| **Pipeline panel** | Per-stage timings for the selected reply. Tap any reply to inspect it |

The transcript is shown in the user's own bubble, so a mishearing is visible
rather than buried — if "delivery report" came through as "delivery force", the
audience sees why the answer was odd.

---

## Demo script

1. **Start with the amber warning.** Say the numbers are demo data. Getting this
   in first is the difference between an honest demo and a misleading one.
2. **Hold the mic, ask in Hinglish:** *"Aaj kitne campaign gaye?"*
3. **Point at the transcript** in your own bubble — that is what the speech engine
   heard.
4. **Tap the reply** and walk through the pipeline panel. The bars are relative, so
   the bottleneck is visible at a glance — usually transcription, not the model.
5. **Ask a follow-up:** *"Aur kal ka?"* — it resolves against the previous turn,
   which shows conversation state working.
6. **Try a wrong campaign name** on purpose. It refuses and lists the real ones
   rather than guessing — worth calling out, because a confidently wrong report is
   worse than an error.

**Do not claim** live-while-recording transcription. The Cloud API only sees a
voice note after it is sent, and this UI works the same way.

---

## The demo API

Three endpoints under `/demo`, mounted only when `DEMO_ENABLED=true`.

| Endpoint | Does |
|---|---|
| `GET /demo/status` | Config for the header. No secrets |
| `POST /demo/ask` | `{question, session_id}` → answer + timing |
| `POST /demo/voice` | multipart audio → transcript + answer + per-stage timings |

### Security

**These endpoints bypass the WhatsApp allowlist**, because an HTTP caller has no
verified phone number to check. Consequences, all deliberate:

- **Off by default.** `DEMO_ENABLED` must be set explicitly.
- **Disabled returns 404, not 403** — a disabled demo should not advertise that it
  exists.
- **`DEMO_TOKEN`** adds a shared secret, sent as `X-Demo-Token`. Set it for
  anything reachable by more than you.
- **CORS is restricted** to `DEMO_ALLOWED_ORIGINS` (the Vite dev server by default).
- The server logs a warning at startup whenever the demo is on.

**Never enable this on a deployment that reaches real data.**

---

## Configuration

Backend (`.env`):
```
DEMO_ENABLED=true
DEMO_TOKEN=                                     # optional shared secret
DEMO_ALLOWED_ORIGINS=http://localhost:5173
```

Frontend (`frontend/.env`):
```
VITE_API_BASE=http://localhost:8000
VITE_DEMO_TOKEN=                                # must match DEMO_TOKEN if set
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Backend unreachable" | Server not running, or `DEMO_ENABLED` not set |
| 401 on every call | `DEMO_TOKEN` set but `VITE_DEMO_TOKEN` missing or different |
| CORS error in the console | UI origin not in `DEMO_ALLOWED_ORIGINS` |
| "Agent is not configured" | `ANTHROPIC_API_KEY` missing, or `HANDLER` is not `agent` |
| Mic button does nothing | Browsers only allow recording on a secure context. `localhost` counts; a LAN IP does not — use localhost, or serve over HTTPS |
| First voice note is very slow | The speech model loads at startup; give the server a moment before demoing |

---

## Known limits

- **Not deployed with the product.** No Dockerfile, no hosting — run it locally.
- **No tests for the React app.** The backend API it calls has 21; the UI itself
  was verified by hand in a browser. For a throwaway presentation tool that is a
  deliberate trade, not an oversight to be surprised by later.
- **Conversation resets on reload** — the session id is generated per page load.
