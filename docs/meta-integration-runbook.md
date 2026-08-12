# Meta / WhatsApp Cloud API — Integration Runbook

**Who this is for:** whoever takes over the Meta side of this project. Everything
here is Meta-specific — tokens, webhooks, the dashboard, and every failure this
integration has actually produced, each with its fix.

Nothing about STT engines, the agent, eko's payment API, or deployment is here.
For those: [phase-3-voice.md](phase-3-voice.md), [phase-4-agent.md](phase-4-agent.md),
[voice-payments.md](voice-payments.md), [phase-6-deploy.md](phase-6-deploy.md).

For the original click-by-click setup, [phase-1-meta-setup.md](phase-1-meta-setup.md)
and [phase-2-webhook.md](phase-2-webhook.md) still apply. **This file is the
troubleshooting and edge-case layer** — read it before you touch the dashboard,
not after something breaks.

---

## §1 — The four values, and which is which

Confusing these is the single most common source of wasted time here, because
three of them look like opaque strings and two are named almost identically.

| `.env` key | Where it comes from | Notes |
|---|---|---|
| `WHATSAPP_ACCESS_TOKEN` | Meta → System Users → Generate token | **Must be a System User token.** See §2. |
| `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp → API Setup, under the "From" number | **Not the phone number.** It is a long numeric ID. |
| `WHATSAPP_VERIFY_TOKEN` | **You invent it.** Meta does not issue it. | Any random string (`openssl rand -hex 16`). Paste the *same* value into Meta's webhook config. |
| `WHATSAPP_APP_SECRET` | App → Settings → Basic → App Secret | Per-app. Used to verify `X-Hub-Signature-256`. |

**Phone Number ID vs phone number**, concretely — in this deployment:

```
WHATSAPP_PHONE_NUMBER_ID = 1195496766984611      ← goes in .env
display_phone_number     = +91 95997 22251       ← what users message
```

### If you create a NEW Meta app

Three values change, one does not:

- ✅ New: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`
- ➖ Unchanged: `WHATSAPP_VERIFY_TOKEN` (it is yours, not Meta's — just re-enter
  the same string in the new app's webhook config)

You must also redo, per app: adding the WhatsApp product, the webhook config, the
`messages` field subscription, and the test-recipient list.

---

## §2 — Tokens: the mistake this project made three times

**A token copied from the WhatsApp → API Setup page is temporary.** It dies in a
few hours to ~24h. This project pasted one in, deployed, and hit `401` three
separate times before switching to a permanent token. Do not repeat it.

### Getting a permanent token

1. Meta **Business Settings → Users → System Users → Add**, create a system user
   with the **Admin** role.
2. **Add Assets** → assign this app, with full control.
3. **Generate token** → select **`whatsapp_business_messaging`** and
   **`whatsapp_business_management`** → copy it. Shown once only.

### Prove it is actually permanent — do not trust the label

```bash
TOKEN='EAA...'
curl -s "https://graph.facebook.com/debug_token?input_token=${TOKEN}&access_token=${TOKEN}"
```

You are looking for exactly this:

```json
{"data":{"type":"SYSTEM_USER","expires_at":0,"is_valid":true,
         "scopes":["whatsapp_business_management","whatsapp_business_messaging",...]}}
```

- `"type":"SYSTEM_USER"` — if it says `USER`, it is a temporary token. Start over.
- `"expires_at":0` — 0 means never. **Any other number means it will die.**
- Both `whatsapp_business_*` scopes present.

This one check would have saved three rounds of debugging. Run it every time
someone hands you a token.

### The token breaks BOTH directions

A dead token does not just stop replies. Downloading the user's voice note is
also an authenticated Graph call, so the symptom is *"the message arrives and
nothing at all happens"* — not *"replies stopped"*. Both of these fail together:

```
GET  /{media_id}                  ← resolving the voice note   → 401
POST /{phone_number_id}/messages  ← sending the reply           → 401
```

---

## §3 — Development vs Live mode is NOT about the token

These are unrelated, and conflating them cost time here too.

| | Controls | Does **not** control |
|---|---|---|
| **Dev / Live mode** | *Who* may message the number. Dev = only pre-registered test recipients. Live = anyone. | Token lifetime |
| **Access token** | Our server's own outbound Graph calls | Who may message you |

**Switching to Live mode does not make a temporary token permanent.** You still
need §2.

In **Development mode**, add every test number under
**WhatsApp → API Setup → "To" → Manage phone number list** and confirm the code
WhatsApp sends. This is Meta's list and is completely separate from this
project's own `WHATSAPP_ALLOWED_NUMBERS` / allowlist file — a number can pass one
and be rejected by the other.

---

## §4 — Webhook setup, and the step everyone forgets

**Meta app dashboard → WhatsApp → Configuration → Webhook → Edit**

- **Callback URL:** `https://<your-host>/webhook` — the `/webhook` path is not
  optional
- **Verify token:** the exact `WHATSAPP_VERIFY_TOKEN` string from `.env`
- Click **Verify and save** → green tick

### ⚠️ Then subscribe to `messages` — this is a separate step

**Configuration → Webhook fields → Manage → subscribe to `messages`.**

Without this, verification passes, you get a green tick, everything *looks*
correct — and not one message is ever delivered. This is the most common silent
failure in the whole integration. If the server logs show no `POST /webhook` at
all, check this first.

### Meta requires publicly-trusted HTTPS

A self-signed certificate is rejected during the handshake. This project's rack
server serves HTTPS on port 443 with a self-signed cert (`/etc/nginx/ssl/recon.crt`,
`server_name _`, IP-based), so **`https://<ip>/webhook` does not work** — no
nginx config will fix that, the certificate itself is the problem.

What is used instead: a Cloudflare quick tunnel from the server.

```bash
cd /home/deepanshu/WA_VOXO
nohup bin/cloudflared tunnel --url http://localhost:8003 --no-autoupdate > cloudflared.log 2>&1 &
grep trycloudflare cloudflared.log     # the URL to paste into Meta
```

> **⚠️ This URL is not stable.** It is a free account-less tunnel running as a
> plain background process, not a systemd service. If `cloudflared` restarts or
> the server reboots, **the URL changes and you must re-enter it in Meta's
> dashboard.** For anything long-lived, use a named Cloudflare Tunnel with a real
> domain, or get a domain + Let's Encrypt certificate on the server.

### The handshake is fussy about the response body

Meta's verification `GET` expects the raw `hub.challenge` string as
`text/plain` — returning JSON fails. This is already handled in
[`whatsapp/app.py`](../src/whatsapp_voice/whatsapp/app.py) (`verify()`), but
worth knowing if you ever reimplement it.

---

## §5 — Verifying each piece independently

Run these top to bottom. Each isolates one link in the chain, so a failure tells
you exactly which step is broken.

```bash
# 1. Is the token valid, and is it permanent?
curl -s "https://graph.facebook.com/debug_token?input_token=${TOKEN}&access_token=${TOKEN}"

# 2. Does the token reach this phone number, and which number is it?
curl -s "https://graph.facebook.com/v21.0/${PHONE_NUMBER_ID}?fields=display_phone_number,verified_name,code_verification_status,quality_rating" \
  -H "Authorization: Bearer ${TOKEN}"

# 3. Is our own server up?
curl -s localhost:8003/health

# 4. Is it reachable from the public internet (i.e. can Meta get to it)?
curl -s https://<tunnel-host>/health

# 5. Does the verification handshake work? Must print the challenge, nothing else.
curl -s "https://<tunnel-host>/webhook?hub.mode=subscribe&hub.verify_token=${VERIFY_TOKEN}&hub.challenge=test123"
```

Healthy output for #2:

```json
{"display_phone_number":"+91 95997 22251","verified_name":"Eko Bharat Venture Pvt Ltd",
 "code_verification_status":"VERIFIED","quality_rating":"GREEN","id":"1195496766984611"}
```

`quality_rating` matters: Meta downgrades it (`GREEN` → `YELLOW` → `RED`) when
users block or report the number, and a `RED` number gets messaging limits.

---

## §6 — Every failure this project actually hit

Ordered by how much time each one cost.

| Symptom | Real cause | Fix |
|---|---|---|
| Message arrives, **nothing happens at all** — no reply, no error to the user | Access token expired. Both media download and send fail together. Logs: `401 — Authentication Error (code 190)` | Permanent System User token, §2. Verify with `debug_token`. |
| `Session has expired on <date>` on a token you *just* created | It was a temporary token from the API Setup page, not a System User token | §2 |
| "Verify and save" fails in Meta's dashboard | Server not running / URL wrong or stale / `/webhook` path missing / verify token mismatch / **self-signed HTTPS** | Run §5 steps 3–5 in order |
| Verification passed (green tick) but **no messages ever arrive** | Not subscribed to the **`messages`** webhook field | §4 |
| Verification fails even though the token matches | Handshake returned JSON instead of the raw challenge as `text/plain` | Already handled; see §4 |
| Webhooks stopped arriving after a reboot or crash | `cloudflared` restarted and the tunnel URL changed | Get the new URL, re-enter in Meta. §4 |
| Logs: `Rejected webhook with invalid signature` | `WHATSAPP_APP_SECRET` is from a different app, or the body was re-serialised before hashing | Copy the secret again from App → Settings → Basic |
| `HTTP 400`, recipient error on send | Number not on Meta's test-recipient list (Dev mode), or not in international format without `+` | §3 |
| Voice note fails, **text works fine** | Not a Meta problem at all — `ffmpeg` missing on the host. `audio.py` shells out to it for every voice note | `sudo apt-get install -y ffmpeg` |
| Two replies to one voice note | Meta retried because our 200 was slow, and dedup is per-process | Run exactly **one** worker; see [phase-6-deploy.md](phase-6-deploy.md) |
| `"I couldn't download that voice note"` | Token expired, **or** the media URL was resolved and then reused later — Meta's media URLs expire in ~5 minutes | Never queue a resolved URL; `fetch_media` resolves and downloads back-to-back |

### Error codes

Only one of these has actually been observed in this project. The rest come from
Meta's published list and are here so you recognise them — treat the causes as
Meta's documentation, not as something verified here.

| Code | Meaning | Seen here? |
|---|---|---|
| `190` | Access token expired / invalid | ✅ **Yes**, three times |
| `131047` | Re-engagement required — outside the 24-hour window (§7) | Not yet |
| `131026` | Message undeliverable (recipient not on WhatsApp, or hasn't accepted terms) | Not yet |
| `131051` | Unsupported message type | Not yet |
| `133010` | Phone number not registered | Not yet |
| `80007` / `131056` | Rate limits | Not yet |

Meta returns a structured error body, and
[`client.py`](../src/whatsapp_voice/whatsapp/client.py) surfaces its `message`
and `code` rather than a bare status — so the log line usually names the problem
directly. Read it before guessing.

---

## §7 — ⚠️ The 24-hour window is NOT handled in this codebase

**The rule:** a business may send free-form messages only within **24 hours** of
the user's last message. Outside that window, Meta requires a **pre-approved
message template**, and a plain send fails with `131047`.

**There is no template handling anywhere in this project.** Not a bug so far,
because the bot is purely *reactive* — every reply goes out seconds after a user
message, so the window is always satisfied. But it is an unguarded assumption,
and two things break it:

1. **`tools/send_hello.py`** sends a message nobody asked for. It works against a
   test number, but from a real business number to someone who has not messaged
   in 24h it will fail with `131047`.
2. **The payment callback flow** (`POST /payments/callback`) sends a receipt when
   the rail calls back. Currently unused (`spoken_pin` is the active mechanism),
   but if this moves to the UPI-link handoff, a callback arriving long after the
   user's last message would be outside the window.

If either becomes real, someone has to create and get approval for a message
template in Meta's dashboard, and add a template-send path to
[`client.py`](../src/whatsapp_voice/whatsapp/client.py).

---

## §8 — Other Meta behaviours worth knowing

Handled in code, but non-obvious enough to trip up the next person.

- **The webhook must return 200 immediately**, before any slow work. Meta retries
  anything that is not a prompt 200, so slow handling turns one voice note into a
  pile of duplicate replies. Handlers run in a background task for this reason.
- **`X-Hub-Signature-256` must be verified against the raw request body.**
  Re-serialising the parsed JSON changes key order and whitespace, producing a
  different MAC that will never match. Compared with `hmac.compare_digest`, not
  `==`, so response timing does not leak the expected value.
- **Media is a two-step fetch** (`GET /{media_id}` → a temporary URL → download),
  the URL expires in **~5 minutes**, and the download itself needs the bearer
  token.
- **Contact cards have an awkward shape** — a list of phones nested inside a list
  of contacts, and either can be empty.
- **A `200` is returned even for payloads we don't recognise.** Status callbacks
  (delivered/read receipts) and unknown shapes are normal traffic, not errors.
- **Groups have hard Meta constraints:** the business must *create* the group (it
  cannot be added to one), max 8 participants, one Cloud API business per group,
  and interactive buttons never work. Meta also sends **no structured "mentions"
  field** for Cloud API groups, which is why the wake word is matched as a plain
  string. Groups are currently disabled (`groups_enabled: false`).
- **API version is pinned** to `v21.0` (`WHATSAPP_API_VERSION`). Meta deprecates
  versions on a schedule; bump it deliberately and re-test, don't let it drift.
- **Live-while-recording transcription is impossible.** The Cloud API only
  delivers a voice note *after* the user finishes sending it. Don't promise it in
  a demo.

---

## §9 — Current state of this deployment

Accurate as of **6 August 2026**. Update this section when it changes.

| Item | Value |
|---|---|
| Meta app | "WA Voxo", app id `2109835886622600` |
| Business number | `+91 95997 22251` (Eko Bharat Venture Pvt Ltd, `VERIFIED`, quality `GREEN`) |
| Phone Number ID | `1195496766984611` |
| Token type | System User, `expires_at: 0` (permanent) ✅ |
| App mode | Live |
| Webhook URL | Cloudflare quick tunnel → `localhost:8003` — **unstable, changes on restart** |
| Signature verification | Enabled (`WHATSAPP_APP_SECRET` set) |
| API version | `v21.0` |
| Groups | Disabled |

Secrets are **not** recorded here — this file is committed to git. All four values
live in `.env` on the server (`/home/deepanshu/WA_VOXO/.env`), which is
gitignored.

### Still open on the Meta side

- [ ] **Stable webhook URL.** The quick tunnel is fine for a pilot and wrong for
      production. Needs a named Cloudflare Tunnel + domain, or a domain with a
      real certificate on the server.
- [ ] **Confirm Official Business Account status.** If the number is not an OBA,
      verification is a 1–2 week process and blocks some features.
- [ ] **Template handling**, if anything ever needs to message a user
      proactively (§7).
- [ ] **`cloudflared` as a service** so a reboot does not silently kill the
      webhook.
