# Phase 6 — Deployment

**Goal:** run without a laptop open.

**Acceptance:** the container serves `/health`, receives a webhook from Meta at a
stable HTTPS URL, and answers a voice note.

---

## ⚠️ Read this before scaling: not everything stateful is shared yet

Several phases added in-memory state. The payment-critical ones are now
store-backed — safe across N workers **once `REDIS_URL` is set** — but a few
lower-severity ones still are not:

| State | Where | With `REDIS_URL` set | Without it (N workers, no Redis) |
|---|---|---|---|
| Deduplication (`SeenMessages`) | `whatsapp/app.py` | ✅ Shared | Each worker has its own set — Meta's retry is processed twice, **the user gets two replies to one voice note** |
| Pending intents (`PendingIntents`) | `agent/payments.py` | ✅ Shared | Worker A prepares the transfer, worker B gets the PIN and finds nothing pending — **the payment simply does not work** |
| Settlement tracking (`SettlementQueue`) | `agent/settlement.py` | ✅ Shared | A transfer eko accepted is never followed up on if the worker tracking it restarts or a later poll lands elsewhere |
| Shared contacts (`ContactBook`) | `agent/payments.py` | ❌ Still per-process | A shared contact is not recognised on another worker, so the digits get read back unnecessarily. Annoying, not unsafe |
| Group wakefulness (`GroupRegistry`) | `whatsapp/group_handler.py` | ❌ Still per-process | A woken group looks dormant to another worker |
| Conversation history (`ConversationStore`) | `agent/conversation.py` | ❌ Still per-process | Follow-ups land on whichever worker answers, so "aur kal?" loses its context roughly `(N-1)/N` of the time |

`guard_worker_count` refuses to start with `WEB_CONCURRENCY>1` and no
`REDIS_URL` at all — it cannot tell that the three shared-safe ones are fine
and only the bottom three degrade, so it fails closed on the worst case.
**Setting `REDIS_URL` makes N workers safe for money and for deduplication.**
The remaining three are a real but lower-severity gap: features work, they
just occasionally forget who they were talking to. Migrating them follows the
same store-backed pattern `PendingIntents`/`SettlementQueue` already use.

---

## Local Docker run

**Verified 2026-07-29** — build and run were exercised end to end, not just
written: `docker build` completes clean (~1.4GB image), the container starts as
`appuser` (not root), `ffmpeg`/`ffprobe` are present, the webhook GET handshake
echoes the challenge correctly, `/health` responds, and Docker's own
`HEALTHCHECK` reports `healthy`. Not yet deployed to a real host — this was a
local build only.

```bash
docker build -t whatsapp-voice .
docker run --rm -p 8000:8000 --env-file .env whatsapp-voice
curl localhost:8000/health
```

The health response tells you what is actually running:

```json
{
  "status": "ok",
  "handler": "AgentHandler",
  "access_control": "none",
  "signature_verification": true,
  "stub_data": true
}
```

`stub_data: true` means every figure in every reply is fake. Check this before any
demo where someone might believe the numbers.

### Image notes

- **`ffmpeg` is a runtime dependency**, not a build one — `audio.py` shells out to
  it for every voice note. It is installed in the runtime stage; without it, voice
  notes fail at conversion, which is a confusing way to discover a missing system
  package.
- **Non-root user** (`appuser`, uid 10001). A compromised dependency should not
  have root.
- **`HF_HOME` is set** to a writable path. Whisper caches models there; on a
  read-only filesystem the first transcription would otherwise fail.
- **Two-stage build** so the compiler toolchain does not ship to production.
- **`.dockerignore` excludes `.env` and `data/`** — secrets must never enter an
  image layer, and `data/` holds customer voice notes.

---

## Choosing a host

The requirement is narrow: a **stable public HTTPS URL** and a long-running
process. Meta will not deliver webhooks to an ngrok URL that changes every restart.

| Option | Fits because | Watch out |
|---|---|---|
| **Render / Railway** | Docker deploy, HTTPS included, free tier for a pilot | Free tiers sleep when idle — a sleeping instance misses webhooks. Use a paid instance for anything real |
| **AWS ECS / Fargate** | If eko already runs on AWS, this inherits the VPC and IAM story | More setup; needs a load balancer for HTTPS |
| **A plain VM + Caddy** | Full control, cheapest at small scale | You own patching and TLS renewal |

**Do not deploy to a platform that scales to zero.** A cold start takes tens of
seconds (the speech model loads at startup), and Meta's retry window is shorter
than that.

### Environment variables to set on the host

Everything from `.env` except that `LOG_FORMAT` should be `json` (the Dockerfile
defaults it), plus:

```
PORT=8000                 # most platforms inject this
LOG_LEVEL=INFO
WHATSAPP_APP_SECRET=...   # REQUIRED here — the endpoint is public
```

There is no allowlist — every number that reaches the deployed URL is
answered, payments included. See [phase-5-auth.md](phase-5-auth.md).

`WHATSAPP_APP_SECRET` is optional locally and **mandatory** in a deployment: it is
what makes signature verification possible. The health endpoint reports
`signature_verification: false` if it is missing — check that after deploying.

---

## Point Meta at it

1. Meta app dashboard → **WhatsApp → Configuration → Webhook → Edit**
2. Callback URL: `https://<your-host>/webhook`
3. Verify token: the same value as `WHATSAPP_VERIFY_TOKEN`
4. Subscribe to the **`messages`** field

Full walkthrough in [phase-2-webhook.md](phase-2-webhook.md); the only change is
the URL.

---

## Logging

`LOG_FORMAT=json` emits one JSON object per line, which is what log aggregators
expect. `text` is for a terminal.

**A redaction filter is installed on both formats.** It masks:

- Meta access tokens (`EAA…`) and API keys (`sk-…`) → `[REDACTED-TOKEN]`
- Phone numbers → last four digits only

It runs on the *formatted* message, so it catches numbers arriving via lazy `%s`
arguments and from third-party libraries, not only our own call sites. `httpx` is
also quietened to WARNING, because at INFO it logs full signed Meta media URLs —
not useful, and not something to retain.

This matters more in a deployment than locally: a log aggregator is the one place
data is retained by default and shared widely.

---

## Cost tracking

Every answered message logs one line with its token counts:

```
Answered …3210 — tools=['get_campaign_count'] tokens=412/38
```

Sum `tokens=in/out` over a day and multiply by the model's rate for a running
total. Per-message cost is dominated by speech-to-text if you are using a paid
engine, not by the model.

There is deliberately no billing integration — at demo and pilot volume the
numbers are small enough that a log query answers the question, and a metrics
pipeline would be scaffolding nobody has asked for yet.

---

## Before a real launch

- [ ] `WHATSAPP_APP_SECRET` set, and `/health` confirms `signature_verification: true`
- [ ] Aware that `/health` reports `access_control: "none"` — there is no allowlist
      (see [phase-5-auth.md](phase-5-auth.md)); anyone who finds the number can
      attempt a payment, up to the amount caps
- [ ] A **permanent** access token (System User), not a 24-hour temporary one — see
      [phase-1-meta-setup.md](phase-1-meta-setup.md)
- [ ] `stub_data: false` — a real `CampaignDataSource` is in place
- [ ] Business verification done, if a real phone number is wanted rather than the
      test number (allow 1–2 weeks)
- [ ] One worker, or `REDIS_URL` set (`guard_worker_count` refuses to start
      otherwise) — see the table at the top of this document for what is and
      is not shared once Redis is in place

---

## Known limits

- **`ContactBook`, `GroupRegistry` and `ConversationStore` are still per-process**
  even with Redis configured — see the table at the top of this document. They
  degrade rather than break.
- **No alerting.** Failures land in logs, and nothing pages anyone.
- **Model weights download on first run** if `STT_ENGINE` falls back to local
  Whisper (e.g. `SARVAM_API_KEY` missing), which makes the first container start
  slow. Not hit with the default `STT_ENGINE=sarvam` config. Pre-baking weights
  into the image would fix it at the cost of a much larger image.
- **Not yet deployed to a real host.** Verified locally only — see the note at
  the top of this document.
