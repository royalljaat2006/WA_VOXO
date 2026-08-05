# Voice-Command Bot with Money Transfer — Feasibility & Design

**Status:** research complete, nothing implemented
**Date:** 2026-08-03
**Scope:** assessing four requested additions to the existing voice agent. No code changed.

Every claim below is tagged **[VERIFIED]** (primary source, cited), **[SECONDARY]** (credible
third party), or **[UNKNOWN]** (must be tested). Nothing is presented as fact on inference alone.

---

## 1. Verdict summary

| # | Requirement | Verdict |
|---|---|---|
| 1 | Voice note → transcribe → **execute money transfer** | ⚠️ **Possible only in modified form.** Voice may create an *intent*; it may **not** authorise the payment. This is a legal constraint in India, not a design preference. |
| 2 | Summon bot into an **existing 1:1 chat** between two users, and dismiss it | ❌ **Not possible.** No API exists. `@Meta AI` is a first-party client feature. |
| 3 | Same summon/dismiss in **group chats** | ✅ **Possible, with hard limits** — via the 2026 Groups API. Bot *creates* the group; users cannot add it to theirs. Max 8 participants. **No interactive buttons in groups.** |
| 4 | Transcribe voice notes | ✅ **Already built and working** (Sarvam, ~0.7s). |

### Correction to an earlier assumption

I previously told you groups were not possible on the Cloud API. **That was wrong.** Meta shipped a
**Groups API in 2026** [VERIFIED — [Meta Groups API docs](https://developers.facebook.com/documentation/business-messaging/whatsapp/groups)].
Requirement 3 is achievable. Requirement 2 remains impossible, for a different reason explained below.

---

## 2. Requirement 1 — money transfer by voice

### The blocking constraint is legal, and it is already in force

**RBI's *Authentication Mechanisms for Digital Payment Transactions Directions, 2025*** — issued
25 September 2025, **enforceable from 1 April 2026** [VERIFIED —
[KPMG India summary](https://kpmg.com/in/en/insights/2025/12/reserve-bank-of-india-rbi-authentication-mechanisms-for-digital-payment-transactions-directions-2025.html),
[Business Standard](https://www.business-standard.com/finance/news/rbi-two-factor-authentication-digital-payments-guidelines-2026-125092501154_1.html)].

It requires, for **every** covered digital payment:

- **At least two factors** from three distinct categories: **Knowledge** (PIN/password),
  **Possession** (a device), **Inherence** (biometric).
- **At least one factor must be dynamically created and unique to that transaction.**

And specifically on our case [SECONDARY — [Medianama, July 2026](https://www.medianama.com/2026/07/223-ixigo-voice-payments-rbi-rules-human-authentication/)]:

> neither the e-mandate rules nor the 2025 directions currently contemplate an AI agent
> initiating a payment on a spoken instruction

A framework for *agentic payments* is reportedly being built but is **not yet in place**. An IMF
note (April 2026) framed the tension: payment rails require deterministic, auditable authorisation;
AI agents behave probabilistically.

**Conclusion: a voice note cannot lawfully authorise a transfer today.** This is not a risk to
manage — it is non-compliance. There is live precedent: **ixigo** is building voice payments in
India and still routes users through a human 2FA step.

### This also matches the project's own documented threat model

`docs/phase-5-auth.md`, written earlier in this project, already reached the same conclusion
independently:

> number-based identity is strong enough to decide "may this person see a delivery report", and
> **not** strong enough to authorise anything that changes state or moves money

That is why `CampaignDataSource` has no write methods. Voice makes it *worse*, not better — a voice
note can be recorded, replayed, or cloned, and an ASR error that turns "200" into "2000" is
recoverable on a report and not on a transfer.

### The design that does work: voice proposes, UPI authorises

The mechanism is the **UPI deep link** — India's native Cloud API payment integration
[SECONDARY — [WatEase WhatsApp Commerce India 2026](https://watease.com/whatsapp-commerce-india)].

```
1. User sends voice note:  "2000 rupaye Ramesh ko bhej do"
2. Sarvam transcribes                                    ← EXISTS (stt/sarvam.py)
3. Agent extracts intent: {amount: 2000, payee: "Ramesh"} ← NEW tool, read-only
4. Agent resolves payee against eko's beneficiary list    ← NEW, exact-match only
5. Bot replies with an explicit read-back:
      "Ramesh Kumar (•••4321) ko ₹2,000 bhejna hai?
       Confirm karne ke liye tap karein — apne UPI PIN se
       authorise karna hoga."          [ UPI link / CTA button ]
6. User taps → THEIR OWN UPI app opens, amount + payee prefilled
7. User enters UPI PIN in their own app                  ← THE authorisation
8. eko's existing payment API executes + confirms         ← eko's stack, not ours
9. Bot posts the receipt back into the chat
```

Why this satisfies the rules:

| Requirement | How it is met |
|---|---|
| RBI: two factors | **Possession** (the device holding the UPI app) + **Knowledge** (UPI PIN) |
| RBI: one dynamic factor | The UPI PIN entry is per-transaction |
| NPCI: only the payer's own app may collect a UPI PIN | We never see or collect it — the deep link hands off |
| WhatsApp policy | We deep-link out; we do not collect payment credentials in chat |
| Project threat model | Voice never authorises. It only proposes. Phone-number identity is never used to move money. |

**The critical property: the voice step is advisory, the UPI step is authoritative.** If someone
replays a stolen voice note, the worst outcome is an unwanted *confirmation prompt* on the owner's
phone — not a completed transfer.

### eko already has the licences and the rails

[SECONDARY — [Wikipedia: Eko India Financial Services](https://en.wikipedia.org/wiki/Eko_India_Financial_Services)]

- **PPI licence** from RBI (25.03.2015)
- **Business Correspondent** for SBI, Yes Bank, ICICI
- Direct NPCI integration; IMPS/NEFT capability
- API platform: ~15,000 agents, 70 partners, ₹8,000cr+ processed

**We are not building payments. We integrate with eko's existing authenticated API.** That
materially reduces scope — and means the new `PaymentGateway` interface should be as thin as
`CampaignDataSource` is.

One caution: RBI fined eko ₹500,000 in 2017 under the Payment and Settlement Systems Act for
non-adherence and inaccurate reporting. **Compliance sign-off is not optional here.**

---

## 3. Requirement 2 — summon into an existing 1:1 chat

**Not possible. No workaround exists.**

`@Meta AI` is built into the WhatsApp *client* by Meta. It is first-party. The Cloud API model is
that a business owns a number, and users message **that number directly**. Webhooks deliver only
messages addressed to the business number
[VERIFIED — [Meta Groups messaging docs](https://developers.facebook.com/documentation/business-messaging/whatsapp/groups/groups-messaging/)
confirms the delivery model and the `group_id` discriminator; there is no API surface for joining a
third party's conversation].

I found **no** evidence of any beta, partner programme, or BSP capability that lets a third-party
bot enter a conversation between two consumer users. Nothing in the Groups API changes this —
Groups lets the **business create a group and invite people**; it does not let the bot into your
existing chat with a friend.

⚠️ **Warning:** searches surface unofficial services (Wassenger, GREEN API, WASenderApi and
similar) advertising group automation and richer control. **These are not the official Cloud API.**
They typically drive a logged-in WhatsApp client, which violates WhatsApp's Terms. For a regulated
entity, using one risks number bans and a compliance finding. Do not use them.

### Closest achievable experiences

| Option | What the user does | Verdict |
|---|---|---|
| **A. Dedicated bot number** (already built) | Messages the bot directly in its own thread | ✅ Works today. Not "in" their other chat, but zero new work. |
| **B. Bot-created group** (Groups API) | Bot creates a small group, invites the user + counterparty | ✅ Closest to the ask. See §4. |
| **C. Forward-to-bot** | User forwards a voice note from any chat to the bot | ✅ Works today, no new code. Genuinely useful and often overlooked. |
| **D. Deep link from any chat** | User taps a `wa.me` link that opens the bot thread | ✅ Trivial. Good for onboarding. |

**Recommendation: A + C now, B when an Official Business Account is in place.** C in particular
gets you most of the perceived value ("use the bot on a message from another chat") for no
engineering cost.

---

## 4. Requirement 3 — group chats via the Groups API

✅ **Possible.** Constraints are significant and shape the design.

[VERIFIED — [Meta Groups API](https://developers.facebook.com/documentation/business-messaging/whatsapp/groups)]

| Property | Value | Consequence for us |
|---|---|---|
| Eligibility | **Official Business Account (OBA) required** | ⚠️ Gating dependency — you do not have this yet |
| Not available for | WhatsApp Business *app* numbers; Multi-solution Conversations numbers | Check your number's onboarding type |
| Joining model | **Invite-only.** Business creates the group, sends an invite link | Users **cannot** add the bot to their own group |
| Max participants | **8** | Fine for a payment between parties; not for broadcast |
| Max Cloud API businesses per group | 1 | No bot-to-bot |
| Max groups per number | 10,000 | Not a constraint |
| Receives others' messages | ✅ Yes, with a `from` field per sender | Multi-party attribution works |
| Webhook discriminator | `group_id` present on group messages | Clean routing — extends `payloads.py` naturally |
| Supported types | Text, media, text/media templates | Voice notes are audio media → **[UNKNOWN]**, see below |
| **NOT supported** | **Interactive messages (buttons, lists)**, auth messages, calling, disappearing, view-once, commerce/cart | ⚠️ **The confirm step cannot use buttons in a group** |
| Group management | Create, delete, invite links, join requests, remove participants, group info, update settings | "Delete group" ≈ the dismiss behaviour you want |

### Two things that change the design

**1. No interactive messages in groups.** Our confirmation step therefore cannot be a button in a
group. It must be a **plain-text UPI deep link** — which still works, and still routes the PIN
entry to the user's own app. In 1:1 chats we *can* use buttons.

**2. [UNKNOWN] Do voice notes arrive in groups?** Docs list "media messages" as supported but do
not name audio explicitly, and there is an `unsupported message type` error (code 130501). **This
must be tested before committing to group voice support.** It is a 30-minute test once an OBA
number exists, and the answer changes the group design materially.

### Dismiss behaviour

"The bot leaves the chat" maps to **delete group** (or **remove participant**) in the Group
Management API. Semantics differ from a human leaving — the bot is the group's owner, so ending the
group is the natural equivalent.

---

## 5. Requirement 4 — voice transcription

✅ **Done.** `stt/sarvam.py` via the `Transcriber` interface, chosen on a real benchmark
(~0.7s, strong Hinglish). Reusable unchanged.

**One carried-forward gap:** Sarvam rejects audio over **30 seconds** (documented in
`docs/phase-0-stt-benchmark.md`). A rambling voice instruction could exceed it. Payment
instructions are usually short, so this is lower risk here than for reporting — but it needs a
clear user-facing error, and chunking or Sarvam's batch API is the eventual fix.

---

## 6. Reuse map

The existing seams absorb most of this. Concretely:

| Component | File | Disposition |
|---|---|---|
| Transcription | `stt/*` | ♻️ **Unchanged** |
| Audio conversion | `audio.py` | ♻️ **Unchanged** |
| Webhook, signature, dedupe, 200-fast | `whatsapp/app.py`, `signature.py` | ♻️ **Unchanged** |
| Agent loop, tool dispatch | `agent/tools.py`, `claude_agent.py`, `nemotron_agent.py` | ♻️ **Unchanged** |
| WhatsApp-safe formatting | `agent/formatting.py` | ♻️ **Unchanged** |
| Allowlist, roles, rate limit | `auth/policy.py`, `guard.py`, `rate_limit.py` | ♻️ **Reused**; add a `payer` role + a per-payee/day cap |
| Conversation state | `agent/conversation.py` | 🔧 **Extend** — hold a pending-intent with a short TTL |
| Message parsing | `whatsapp/payloads.py` | 🔧 **Extend** — read `group_id`; add `GroupMessage` |
| Cloud API client | `whatsapp/client.py` | 🔧 **Extend** — interactive messages (1:1), group send, group management |
| Handler | `whatsapp/handlers.py` | ➕ **New** `PaymentIntentHandler` extending `AgentHandler` (same `_on_transcript` hook Phase 4 used) |
| Payment boundary | `agent/payments.py` | ➕ **New** — a thin `PaymentGateway` protocol, mirroring `CampaignDataSource` |
| Intent tools | `agent/payment_tools.py` | ➕ **New** — `resolve_payee`, `prepare_transfer`. **Read-only + prepare-only. No execute tool reachable by the model.** |

### The single most important design rule

**The model must never have a tool that moves money.** Its most powerful tool returns a *prepared,
unsigned intent*. Execution happens only after the UPI callback, in code the model cannot reach.
This preserves exactly the property that made `CampaignDataSource` read-only, and it makes prompt
injection via a transcript unable to cause a payment.

Payee resolution must be **exact-match, not fuzzy** — the same rule already enforced for campaign
names, and for the same reason: ASR mangles names, and a confidently wrong payee is far worse than
an error. `docs/phase-4-agent.md` records that Nemotron already showed a tendency to substitute a
near-match silently. On a transfer, that is money to the wrong person.

---

## 7. Risks, ranked

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **Shipping voice-authorised payment** | 🔴 Regulatory breach | Voice never authorises. UPI PIN in the user's own app, always. |
| 2 | **Wrong payee from ASR error** | 🔴 Money to a stranger | Exact-match only; read back full name + masked account; require explicit confirm |
| 3 | **Wrong amount from ASR error** ("200"→"2000") | 🔴 | Read amount back in words *and* digits; per-transaction cap |
| 4 | **Compliance not consulted** (RBI penalty history) | 🔴 | Sign-off gate before any real-money flow. Start this now. |
| 5 | **Prompt injection via transcript** | 🟠 | No execute tool exists for the model to reach |
| 6 | **Webhook replay → double payment** | 🟠 | Idempotency key per intent; existing dedupe covers delivery, not intent |
| 7 | **WhatsApp policy violation → WABA ban** | 🟠 | Deep-link out; never collect credentials in chat; no unofficial APIs |
| 8 | **Stolen/borrowed handset** | 🟠 | Unchanged by us — UPI PIN is the barrier, as with any UPI app |
| 9 | **No OBA yet → groups blocked** | 🟡 | Start OBA verification immediately (long lead time) |
| 10 | **Voice notes may not arrive in groups** | 🟡 | Test early; fall back to text-in-group |
| 11 | **Sarvam 30s cap** | 🟡 | Clear error message; chunking later |
| 12 | **Nemotron latency 3.9–42s** | 🟡 | Already documented; switch to `nemotron-3-super-120b-a12b` or Claude for this path |

---

## 8. Open questions — blocking, in priority order

1. **Which eko API executes a transfer, and what auth does it already require?** Determines whether
   the UPI deep-link handoff is a thin wrapper or net-new work.
2. **Is there an eko mobile app to deep-link into?** If yes, that is a stronger and simpler
   authorisation surface than a raw UPI intent link.
3. **Who signs off on compliance, and what is their lead time?** Given the 2017 RBI penalty,
   assume this is a hard gate, not a formality.
4. **Is your WhatsApp number an Official Business Account?** If not, groups are blocked until it is.
   Also confirm it was not onboarded as a Business *app* number or Multi-solution Conversations.
5. **Who are the payees?** A closed set of pre-registered beneficiaries is dramatically safer (and
   more shippable) than arbitrary "this person".
6. **Is eko a UPI TPAP, or does it move money via IMPS/NEFT under its PPI/BC licence?** Changes
   which handoff is correct.
7. **What per-transaction and per-day caps does compliance require?**

---

## 9. Bottom line

- **Requirement 4** is done.
- **Requirement 3** is achievable once you have an Official Business Account — with real limits
  (8 participants, no buttons, bot must create the group).
- **Requirement 2** is not achievable. Offer forward-to-bot and a dedicated thread instead.
- **Requirement 1** is achievable **only** as voice-proposes / UPI-authorises. Voice-authorised
  transfer is not lawful in India today. The good news: eko already holds the licences and the
  rails, so the work is integration, not payments.

The architecture already in place — `Transcriber`, `Agent`, `ToolRegistry`, `GuardedHandler`,
read-only data boundary — absorbs all of this without reopening the transport layer.

See [voice-command-bot-plan.md](voice-command-bot-plan.md) for phasing.
