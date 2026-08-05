# Phase 2 — Webhook Server

**Goal:** two-way messaging. Text the number, the server receives it, an echo comes back.

**Acceptance:** send "hello" from your phone → reply "Echo: hello" arrives.

Phase 1 proved we can *send*. This phase proves we can *receive* — which is the
half that needs a public HTTPS URL, because Meta pushes messages to us.

---

## Prerequisites

- Phase 1 complete (`tools/send_hello.py` worked)
- [ngrok](https://ngrok.com/download) installed (`brew install ngrok`) and a free
  account — needed to give localhost a public HTTPS address

---

## Two extra values in `.env`

Phase 1 needed the token and phone number ID. Receiving needs two more:

```
WHATSAPP_VERIFY_TOKEN=any-random-string-you-invent
WHATSAPP_APP_SECRET=<from Meta app → Settings → Basic → App Secret>
```

**`WHATSAPP_VERIFY_TOKEN`** is *not* issued by Meta — you invent it. You paste the
same string into Meta's webhook config, and the server checks the two match during
the one-time handshake. Any random string works (`openssl rand -hex 16`).

**`WHATSAPP_APP_SECRET`** is issued by Meta. It verifies that incoming webhooks
really came from them — the endpoint is public, so without this anyone who learns
the URL can POST fake messages. The server runs without it (with a loud warning)
so you can test quickly, but set it before deploying anywhere.

---

## Steps

### 1. Start the server
```bash
.venv/bin/python tools/run_server.py
```
It listens on `http://localhost:8000`. Check `http://localhost:8000/health`
returns `{"status": "ok"}`.

### 2. Expose it publicly
In a second terminal:
```bash
ngrok http 8000
```
Copy the `https://` forwarding URL (e.g. `https://a1b2c3.ngrok-free.app`).

> ⚠️ The free ngrok URL **changes every restart**. When it does, update the
> callback URL in Meta (step 3) or webhooks stop arriving.

### 3. Point Meta at it
1. Meta app dashboard → **WhatsApp → Configuration**.
2. Under **Webhook**, click **Edit**.
3. **Callback URL:** `https://<your-ngrok-id>.ngrok-free.app/webhook`
   (note the `/webhook` path — it is not optional)
4. **Verify token:** the exact string you put in `WHATSAPP_VERIFY_TOKEN`.
5. Click **Verify and save**. Meta sends a `GET` immediately; the server logs
   `Webhook verification succeeded` and Meta shows a green tick.
6. Still on Configuration, under **Webhook fields**, click **Manage** and
   **subscribe to `messages`**. Without this, verification passes but no messages
   are ever delivered — the single most common reason this phase appears to work
   and doesn't.

### 4. Test it
Send "hello" from your phone to the test number. You should see:
- a log line: `Queued TextMessage from 91…`
- a reply on your phone: `Echo: hello`

Send a voice note too — you'll get an acknowledgement saying transcription arrives
in Phase 3. **That confirms the voice pipeline's plumbing is in place.**

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "Verify and save" fails | Server not running, ngrok URL wrong or stale, `/webhook` path missing, or the verify token doesn't match `.env` exactly |
| Verification passed, but no messages arrive | You didn't subscribe to the **`messages`** webhook field (step 3.6) |
| Server logs `Rejected webhook with invalid signature` | `WHATSAPP_APP_SECRET` doesn't match the app. Copy it again from Settings → Basic |
| Message received but no reply | Reply send failed — check the log for the `Handler failed` traceback; usually an expired access token |
| Replies arrive twice | Duplicate delivery got past dedup — expected only if the server restarted between the two attempts (the seen-list is in memory) |
| ngrok "too many connections" | Free tier limit; restart ngrok |

---

## How it works

```
Meta ──POST /webhook──▶ verify signature ──▶ parse ──▶ dedupe ──▶ 200 OK (immediately)
                                                          │
                                                          └──▶ background: handler replies
```

Three design rules, each guarding against a specific failure:

1. **Always return 200, fast.** Meta retries any non-200 with backoff, so a slow or
   failing handler turns one message into a pile of duplicates. Real work
   (transcription, API calls) happens after the response is sent.
2. **Verify the signature.** HMAC-SHA256 over the *raw* body — re-serialising the
   JSON changes the bytes and the check would never pass.
3. **Deduplicate by message id.** Delivery is at-least-once; without this, one
   voice note can produce two replies.

The handler is swappable (`src/whatsapp_voice/whatsapp/handlers.py`): Phase 2 uses
`EchoHandler`, Phase 3 will pass a transcribing handler to the same server, and
Phase 4 an agent. **The server itself does not change again.**

---

## What's NOT in Phase 2

- Transcription — the echo deliberately does not transcribe voice notes; that's
  Phase 3, and keeping them separate means a failure here is unambiguous.
- Permanent hosting — ngrok is fine until Phase 6.

Once the echo works, confirm before starting **Phase 3 (voice pipeline)**.
