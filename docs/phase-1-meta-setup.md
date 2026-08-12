# Phase 1 — Meta / WhatsApp Cloud API Setup

**Goal:** get working Cloud API credentials and prove them by sending one message
to your own phone.

**Acceptance:** `python tools/send_hello.py <your-number>` lands a message on your phone.

These are steps **only you can do** — they involve a Meta account and your own
credentials. The code that uses the credentials is already built and tested; this
runbook just gets you the values that go in `.env`.

> **Taking over this project, or hitting an error?** Read
> **[meta-integration-runbook.md](meta-integration-runbook.md)** — it collects every
> Meta-side failure this project has actually produced with its fix, the token trap
> that cost three rounds of debugging, and the webhook step that fails silently.
> This page is the happy path; that one is everything else.

---

## What you'll end up with

Two values in `.env`:

```
WHATSAPP_ACCESS_TOKEN=EAAG...        # temporary (~24h) to start
WHATSAPP_PHONE_NUMBER_ID=123456789   # the test number's ID, not the number itself
```

---

## Steps

### 1. Meta developer account + app
1. Go to <https://developers.facebook.com> and log in (or register — free).
2. **My Apps → Create App** → choose the **Business** type.
3. Give it a name (e.g. "eko-voice-agent") and create it.

### 2. Add WhatsApp
1. On the app dashboard, find **WhatsApp** in the product list → **Set up**.
2. Meta creates a free **test phone number** for you automatically. This is the
   number your agent sends *from*. You don't need to buy or verify a number yet.

### 3. Grab the credentials
On **WhatsApp → API Setup** you'll see:
- **Temporary access token** — copy it into `WHATSAPP_ACCESS_TOKEN`. It expires in
  ~24 hours; fine for testing, we'll set up a permanent one before the demo.
- **Phone number ID** — the value under the "From" number (a long number, *not*
  the phone number). Copy it into `WHATSAPP_PHONE_NUMBER_ID`.

### 4. Add your own number as a recipient
The test number can only message numbers you explicitly allow.
1. On the same **API Setup** page, under **To**, click **Manage phone number list**.
2. Add your personal WhatsApp number and confirm the code WhatsApp sends you.

### 5. Fill in `.env`
```
WHATSAPP_ACCESS_TOKEN=<the temporary token>
WHATSAPP_PHONE_NUMBER_ID=<the phone number ID>
```

### 6. Send the test message
```bash
.venv/bin/python tools/send_hello.py <your-number-in-international-format>
```
e.g. `.venv/bin/python tools/send_hello.py 919876543210` (no `+`, include country code).

A message should arrive on your phone within a few seconds. **That's Phase 1 done.**

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `HTTP 401` / code 190, "Session expired" | Token expired (temporary tokens last ~24h). Copy a fresh one from API Setup. |
| `HTTP 400`, recipient error | Number not in the allowed list (step 4), or not in international format without `+`. |
| `WhatsApp is not configured` | `.env` is missing the token or phone number ID. |
| Nothing arrives, but the script says ✓ | Check the recipient number is the one you registered in step 4. |

---

## About tokens (read before the demo)

The temporary token is fine for today but dies in ~24h. Before a real demo, create
a **permanent token**:

1. Meta **Business Settings → Users → System Users → Add**, create a system user
   with an Admin role.
2. **Add Assets** → assign your app with full control.
3. **Generate token** → select the `whatsapp_business_messaging` and
   `whatsapp_business_management` permissions → copy the token (shown once).

Put that in `WHATSAPP_ACCESS_TOKEN` instead. It doesn't expire.

---

## What's NOT in Phase 1

- **Receiving** messages (webhooks) — that's Phase 2.
- **Business verification / a real number / blue tick** — only needed for a public
  launch, not for the demo. The test number is enough to build everything.

Once the hello message arrives, record it in the decision log and move on to
**Phase 2 (webhook server)** — but confirm before starting it.
