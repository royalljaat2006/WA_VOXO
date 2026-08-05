# Phase 5 — Authorisation

**Goal:** only the right people get answers.

**Acceptance:** an unlisted number gets a refusal and nothing else runs; a listed
number gets answers until it hits its rate limit.

**This phase must land before any real campaign data does.** Until it does, anyone
who discovers the number gets internal reporting.

---

## The threat model, stated plainly

Being explicit about this matters more than the code, because it determines what
the system is allowed to do.

**What WhatsApp guarantees:** the sender's phone number is genuine. Meta verified
it, and it cannot be spoofed on an inbound message. So *"which number sent this"*
is trustworthy — that is a real security property, not an assumption.

**What it does not tell us:** who is holding that phone. A borrowed, stolen, or
unlocked handset is indistinguishable from its owner.

**Therefore:** number-based identity is strong enough to decide *"may this person
see a delivery report"*, and **not** strong enough to authorise anything that
changes state or moves money.

That is why `CampaignDataSource` has no write methods and why there is no write
role. It is not an oversight to be filled in later — it is the conclusion of the
threat model. If a write operation is ever wanted, it needs a second factor, not a
larger allowlist.

---

## Setup

In `.env`:

```
WHATSAPP_ALLOWED_NUMBERS=919876543210:admin,919000000001:analyst,919000000002
```

- Format is `number:role`, comma-separated.
- A **bare number** gets `AUTH_DEFAULT_ROLE` (default `viewer`) — least privilege,
  so a typo cannot silently grant admin.
- Formatting is forgiven: `+91 98765-43210` matches `919876543210`.
- A **mistyped role fails at startup** rather than quietly downgrading access.

An **empty allowlist denies everyone.** Forgetting to configure auth locks the bot
down rather than opening it up; the server logs a warning saying so.

---

## Roles

| Role | Sees |
|---|---|
| `viewer` | Aggregate numbers — campaign counts, overall rates |
| `analyst` | The above, plus per-campaign detail |
| `admin` | All reporting data |

All three are **read-only**. `admin` means "sees everything", not "can do
everything".

Roles are assigned and checked; the tools do not yet branch on them, because every
current tool is safe at `viewer` level. When a tool is added that exposes something
narrower — customer-level data, financial detail — gate it with
`caller.may(Role.ANALYST)`. The plumbing is in place for that.

---

## Rate limiting

Default: **20 messages per caller per hour.**

Two reasons, and only the second is about abuse:

1. **Cost.** Every message spends speech-to-text minutes and LLM tokens. A phone
   left face-down in a pocket, sending voice notes into a chat, is a real way to
   run up a bill overnight.
2. **Containment.** If a handset is lost, a limit bounds how much can be pulled out
   of it before someone notices.

It is a **sliding** window, not a fixed one — a fixed window would let a caller
send the whole quota at 10:59 and the whole quota again at 11:00, which is exactly
the burst it is meant to prevent.

Rejected attempts are **not** counted. Otherwise a caller retrying during cooldown
would keep pushing their own window forward and never get back in — a lockout
instead of a limit.

```
AUTH_RATE_LIMIT=20
AUTH_RATE_LIMIT_WINDOW_S=3600
```

---

## How it is wired

`GuardedHandler` wraps whatever `HANDLER` selects:

```
webhook → GuardedHandler → EchoHandler / TranscribeHandler / AgentHandler
                 │
                 └── denied? reply and stop
```

A wrapper rather than a check inside each handler, for three reasons: auth applies
identically to every handler, it **cannot be forgotten** when a new handler is
written, and there is exactly one place for an audit to point at.

**A denied message goes no further** — no media download, no transcription, no
model call. Cost and data exposure both stop at the gate. There is a test asserting
the inner handler is never reached.

### Two deliberate choices in the guard

**Identity is checked before rate.** Checking rate first would let unknown numbers
consume limiter memory, and it would leak: a different reply for "too many
requests" versus "unknown" tells a prober their number is recognised.

**The refusal says nothing.** An unknown caller gets `"Sorry, I can't help with
that."` — no mention of eko, of an allowlist, of what the service does, or who to
contact. Someone who has found the number should not be able to confirm they have
found an eko internal system. There is a test asserting the message leaks none of
those words.

---

## Logs

Numbers are masked to the last four digits:

```
WARNING  Denied unknown number …3210
WARNING  Rate limited …3210
```

Full phone numbers in log files are a data-protection problem of their own; the
last four are enough to correlate with a support conversation.

---

## Local testing

To test against your own number without maintaining an allowlist:

```
AUTH_ENABLED=false
```

The server logs a loud warning. **Never run this way with real data** — it answers
every number that reaches it.

---

## Known limits

- **In-memory rate limiting.** Each process keeps its own counts, so N workers
  means an effective limit of `limit × N`. Shared state (Redis) is a Phase 6
  prerequisite — see [phase-6-deploy.md](phase-6-deploy.md).
- **Allowlist lives in `.env`.** Fine for a handful of people; a real rollout wants
  it in a database with an admin UI, which is a product decision rather than a
  technical one.
- **No audit log yet.** Every answer logs its tools and token counts, but there is
  no durable record of who asked what. A fintech deployment will want one.
- **Phone-number identity only.** See the threat model above — this is the ceiling
  on what the current design can safely authorise.

---

## Next

Phase 6 ([deploy](phase-6-deploy.md)) — run it somewhere other than a laptop.
