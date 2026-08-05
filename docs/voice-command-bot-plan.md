# Voice-Command Bot — Delivery Plan

**Companion to:** [voice-command-bot-feasibility.md](voice-command-bot-feasibility.md)
**Date:** 2026-08-03 · **Constraint:** very short deadline · **Rule:** reuse existing code

---

## The honest headline

**You can demo the complete concept in ~3 days. You cannot ship real money movement in ~3 days.**

That is not a code limitation. Voice-authorised payment is non-compliant in India under RBI's
*Authentication Mechanisms Directions, 2025* (in force since 1 April 2026), and eko's compliance
function has to sign off on any real-money path. Neither is something engineering can compress.

So the plan splits cleanly:

| | Ships fast | Blocked on others |
|---|---|---|
| **What** | Full voice → intent → confirm → **sandboxed** payment, plus groups | Real money movement |
| **Why** | No regulated action occurs; reuses existing seams | RBI 2FA, compliance sign-off, eko API access, OBA |
| **Time** | ~3 days | Weeks, not in your control |

A sandboxed demo is not a lesser deliverable. It proves every hard part — Hinglish voice
comprehension, intent extraction, payee resolution, the confirmation UX, the handoff — with the
final execution stubbed and clearly labelled. That is exactly how the existing project already
demos campaign reporting with `StubCampaignSource`.

---

## Track 0 — start today, these have the longest lead time

**None of these are code. Start all four in parallel, now.** Every day of delay here is a day added
to the real-money date.

| # | Action | Who | Why it blocks |
|---|---|---|---|
| 0.1 | **Open the compliance conversation.** Bring §2 and §7 of the feasibility doc. Ask: what authorisation do you require, what caps, what audit trail? | You → eko compliance | Hard gate on real money. RBI fined eko ₹5L in 2017 — this will get scrutiny. |
| 0.2 | **Request eko payment API access + docs.** Which endpoint executes a transfer, what auth does it need, is there a sandbox? | You → eko platform | Determines whether Phase 3 is a thin wrapper or net-new |
| 0.3 | **Check / start Official Business Account verification.** Confirm your number is not a Business *app* number or Multi-solution Conversations. | You → Meta Business | Groups API is OBA-only. Verification takes 1–2 weeks. |
| 0.4 | **Get a fresh WhatsApp access token** (System User, permanent). Current one in `.env` expired 29 July. | You | Blocks *all* real WhatsApp testing, including today's phases |

> 0.4 is 10 minutes and unblocks four already-built phases. Do it first.

---

## Phase 1 — Voice → payment intent → confirmation (sandboxed)

**Goal:** a voice note produces a correctly-parsed transfer intent and a confirmation prompt. No
money moves.

**Time:** ~1.5 days · **Depends on:** nothing (works with the demo UI today)

**Deliverables**

- `agent/payments.py` — `PaymentGateway` protocol + `StubPaymentGateway`. Mirrors
  `CampaignDataSource` exactly: thin, and **prepare-only — no `execute` method the model can reach**.
- `agent/payment_tools.py` — `resolve_payee`, `prepare_transfer`. Both read-only.
  **Exact-match payee resolution only**, per the existing campaign-name rule.
- `agent/conversation.py` — extend to hold a pending intent with a short TTL (~2 min) and a
  single-use idempotency key.
- `whatsapp/handlers.py` — new `PaymentIntentHandler` extending `AgentHandler`, using the same
  `_on_transcript` hook Phase 4 introduced.
- Tests: amount/payee extraction, ASR-error cases ("200" vs "2000"), unknown payee refusal,
  intent expiry, replay rejection.

**Acceptance**

- "2000 rupaye Ramesh ko bhej do" → intent `{amount: 2000, payee: Ramesh Kumar (•••4321)}`
- Read-back shows amount **in digits and words**, payee full name + masked account
- Unknown/misheard payee → refuses and lists valid payees; **never substitutes a near match**
- Expired or replayed intent → rejected
- **Nothing executes.** `stub_payment: true` surfaces on `/health`, mirroring `stub_data`

---

## Phase 2 — Confirmation handoff (UPI deep link)

**Goal:** the confirm step routes authorisation to the user's own UPI app. Still sandboxed.

**Time:** ~1 day · **Depends on:** 0.4 (token); answers to open questions 1, 2, 6

**Deliverables**

- `whatsapp/client.py` — extend with interactive messages (CTA URL button) for **1:1 chats**
- Plain-text UPI deep link fallback for **groups** (groups do not support interactive messages)
- A callback endpoint to receive the payment result, with idempotency
- Tests: link construction, callback idempotency, out-of-order/duplicate callbacks

**Acceptance**

- 1:1: confirmation arrives as a tappable CTA button
- Group: confirmation arrives as a tappable link
- Tapping opens the UPI app with amount + payee prefilled
- **We never see or collect the UPI PIN** — verified by inspecting the payload
- A duplicate callback does not produce a second payment

---

## Phase 3 — Groups

**Goal:** bot creates a small group, transcribes voice notes in it, runs the intent flow there.

**Time:** ~1.5 days · **Depends on:** **0.3 (OBA)** — hard blocker

**Do this first, before writing group code:** send a voice note into a test group and confirm it
reaches the webhook. Meta's docs list "media messages" as supported but do not name audio, and
there is an `unsupported message type` error (130501). **[UNKNOWN] in the feasibility doc.** 30-minute
test; the answer changes the design.

**Deliverables**

- `whatsapp/payloads.py` — read `group_id`; add `GroupMessage`
- `whatsapp/client.py` — group create / invite link / delete (the "bot leaves" behaviour) / send
- `auth/` — group-aware authorisation: **who in a group may initiate a payment?** Default: only the
  group creator, and only for their own payees.
- Tests: group vs 1:1 routing, per-sender attribution, non-authorised participant refused

**Acceptance**

- Bot creates a group and sends a working invite link
- Voice note from a participant is transcribed and attributed to the right sender
- A participant who is not the authorised payer cannot initiate a transfer
- "Dismiss" ends the group

**Constraints to design around** (all verified): max 8 participants · bot must create the group,
users cannot add it · no interactive buttons · 1 Cloud API business per group.

---

## Phase 4 — Real money

**Goal:** actual transfers via eko's authenticated API.

**Time:** ~2–3 days of engineering, **gated on approvals that take weeks**
**Depends on:** 0.1 (compliance sign-off), 0.2 (eko API access), Phases 1–3

**Deliverables**

- `EkoPaymentGateway` implementing `PaymentGateway`
- Durable **audit log**: who instructed what, the transcript, the intent, the authorisation result,
  timestamps. This is a regulatory obligation, not a nice-to-have.
- Caps enforced per compliance: per transaction, per day, per payee
- `auth/policy.py` — add a `payer` role; **keep every existing role read-only**
- Runbook + rollback procedure

**Acceptance**

- Compliance sign-off **in writing**
- A real ₹1 transfer completes end to end
- Audit log reconstructs the full chain of consent for that transfer
- Caps demonstrably reject an over-limit instruction

---

## 🚫 Must not ship without compliance sign-off

- Any path where a **voice note alone** results in money moving
- Any step where **we** collect a UPI PIN, card detail, or password
- Real-money flows **without** a durable audit trail
- Any **unofficial WhatsApp API** (Wassenger, GREEN API, WASenderApi and similar) — these drive a
  logged-in client, violate WhatsApp's Terms, and risk a number ban plus a compliance finding
- Removing the read-back / explicit-confirm step to "make it feel faster"

---

## Realistic timeline

```
Day 0   Track 0 all four started (0.4 done in 10 min)
Day 1   Phase 1 — intent extraction + confirmation logic
Day 2   Phase 1 done, Phase 2 — UPI deep-link handoff
Day 3   Phase 2 done → DEMOABLE END TO END (sandboxed)
        Phase 3 starts if OBA has landed; otherwise blocked
+1-2wk  OBA verification completes → Phase 3
+?      Compliance sign-off + eko API → Phase 4
```

**Assumptions this rests on:** you get a working token on day 0; eko already has a transfer API with
its own auth (not net-new); payees are a pre-registered closed set; no new STT or agent work is
needed (both already work).

**If any of those is false, Phase 1–2 still hold** — they are deliberately independent of eko and
Meta.

---

## What to say in the demo

Lead with the honest framing; it is stronger than overclaiming:

> "The voice half is real and working — Hinglish in, correct intent out, in under a second of
> transcription. What voice deliberately does *not* do is authorise the payment. RBI's 2025
> authentication directions require two factors with one unique per transaction, and a spoken
> instruction is not one of them. So voice proposes, and the user authorises with their UPI PIN in
> their own app. That is the same architecture ixigo is using for voice payments in India."

Then show it. The payment is sandboxed and labelled as such — exactly as the existing campaign demo
labels `stub_data: true`.

---

## Open questions, ranked by how much they block

1. Which eko API executes a transfer, and what auth does it already require? → **blocks Phase 4**
2. Is your number an Official Business Account? → **blocks Phase 3**
3. Who signs off compliance, and what is their lead time? → **blocks Phase 4**
4. Is there an eko mobile app to deep-link into? → **changes Phase 2 design**
5. Are payees a closed pre-registered set, or arbitrary? → **changes Phase 1 risk profile sharply**
6. Is eko a UPI TPAP, or moving money via IMPS/NEFT under PPI/BC? → **changes Phase 2 handoff**
7. What caps does compliance require? → **needed for Phase 4**

Answers to 1, 2 and 5 would let me start Phase 1 immediately with no guesswork.
