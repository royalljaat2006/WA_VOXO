# Phase 5 — Authorisation (removed)

This phase built a number allowlist (`auth/policy.py`), a file-backed roster
editable via WhatsApp admin commands (`auth/roster.py`, `auth/admin_flow.py`),
per-caller rate limiting (`auth/rate_limit.py`), and a `GuardedHandler` wrapping
every other handler with the check, plus a separate `+pay` capability flag
gating who could even attempt a transfer (`Caller.can_pay`, checked in
`agent/payment_tools.py`).

**All of it has been removed, deliberately, at the repo owner's explicit
request**, to keep the ability to build per-payer credentials (see below)
simple rather than layering it under an allowlist. There is no replacement
gate — `build_handler` in `whatsapp/app.py` returns the configured handler
directly, wrapped only in `GroupHandler` when groups are on. Every number that
messages this deployment is answered, payments included, subject only to the
amount caps and PIN in `agent/payment_flow.py`. `/health` reports this
explicitly as `"access_control": "none"`.

## What this means operationally

- **There is no allowlist.** Any number that can message the WhatsApp Business
  number can talk to whichever handler `HANDLER` selects.
- **There is no `+pay` flag.** Any number that can message the bot can attempt
  a transfer, gated only by the amount caps (`PAYMENT_MAX_PAISE`,
  `PAYMENT_FIRST_TIME_CAP_PAISE`, `PAYMENT_DAILY_CAP_PAISE`) and the spoken PIN
  eko verifies.
- **There is no per-caller rate limit.** Only the daily payment cap and eko's
  own limits bound repeated use.
- **There are no WhatsApp-driven admin commands** (`/adduser`, `/removeuser`,
  `/listusers`) — there is nothing left for them to edit.

## Why this is a real risk, not just a simplification

Every payment currently draws from **one fixed eko account**
(`EKO_CUSTOMER_ID`/`EKO_INITIATOR_ID`/`EKO_USER_CODE` in `.env`) — see
`EkoSettings` in `config.py`. Removing the allowlist means anyone who finds
this WhatsApp number, not just its intended users, can attempt to spend from
that one account, up to the caps, by guessing or knowing a PIN. There is no
identity check standing between "found the number" and "can try to pay."

If real access control is wanted again, the right shape going forward is
**per-payer eko credentials** (each payer's own eko customer registration,
looked up by WhatsApp number) rather than the old admin-managed allowlist —
see the "who is allowed to try" discussion in this project's chat history for
the tradeoffs between that and a UPI-link handoff (`agent/handoff.py`,
`HandoffKind.UPI_LINK`), which sidesteps the question entirely by having the
payer's own bank authorise in their own app.
