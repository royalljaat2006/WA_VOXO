# Payment errors — a reader's reference

Every message a payer, an operator, or the log can see while a transfer moves
through the system, grouped by where it comes from. Written so a new failure
can be looked up here first — check `git log -p -- docs/payment-errors.md`
for when an entry was added, and the file it points at for the full story.

Real messages are Hinglish because that is what the payer sees on WhatsApp;
operator-facing ones (log lines, tool errors the model sees) are plain
English.

## 1. Before a transfer is even prepared — `resolve_payee` / `prepare_transfer`

These come back as a `ToolError` from `agent/payment_tools.py`. The model
sees the text and turns it into a question for the payer — you will not see
these verbatim in WhatsApp, but they explain *why* the bot asked again.

| Message (starts with) | Cause | What to do |
|---|---|---|
| `'<x>' na UPI ID jaisa lagta hai na 10-digit mobile number jaisa` | The payee text is neither a parseable VPA nor a bare mobile — `parse_vpa` and `parse_mobile_payee` both returned `None`. | Ask the payer to repeat the UPI ID or number clearly. Deliberate: a misheard character here sends money to a stranger. |
| `'<x>' se amount samajh nahi aaya` | `parse_amount_paise` couldn't extract exactly one amount (nothing recognisable, or an ambiguous phrase like `"2000 ya 3000"`). | Ask for the amount again, digits if possible. |
| `₹X is above the per-transaction limit of ₹Y` | `amount_paise > max_amount_paise` (`PaymentSettings`, default ₹10,000). | Hard cap — payer must send in smaller pieces. |
| `No verified payer for this conversation` | `current_payer` context var was never set — a code path called the agent without establishing who is speaking. | Should never happen in production; if it does, something upstream of `PaymentFlow` is broken. |
| `This is the first transfer to <display>, so the limit is ₹Y` | `resolved.is_new` and amount exceeds `first_time_cap_paise` (default ₹500). Applies to *any* payee the directory can't confirm — new VPA, new mobile, name-only VPA. | First transfer to an unconfirmed payee must be small; suggest a smaller amount. |
| `Aaj ki limit ₹X hai. ₹Y already sent today...` | Daily cumulative cap (`daily_cap_paise`, default ₹25,000) would be exceeded. | Tell the payer the remaining amount; do not silently retry smaller. |
| `recipient_name is empty` / `'<x>' is not a name — it has no letters` / `'<x>' looks like a UPI ID, not a name` | `_validated_recipient_name` rejected what the model passed as `recipient_name` — eko rejects an empty name outright (see §3, status 1449). | Ask the payer whose name the money is going to; never invent one. |
| `'<phone>' kisi bhi contact se match nahi hua. Available: ...` | **Stub gateway only** (`StubPaymentGateway.resolve_payee`) — the dictated name/number isn't in the fake directory. Real eko never raises this; it has no name lookup at all. | Demo/test-mode only. |

## 2. The PIN step — `agent/payment_flow.py`

These *are* exactly what the payer reads on WhatsApp, since this stage is
never touched by the model.

| Message | Meaning | Audit `reason=` | Notes |
|---|---|---|---|
| `Transfer cancel kar diya. ❌` | Payer said "cancel" while a PIN was pending. | `user_cancelled` | Normal, not an error. |
| `Pichhla transfer cancel ho gaya.` (prefix) | Payer sent a message that is **neither the PIN nor a recognised cancel word** while a transfer was pending — e.g. asked something unrelated, or started dictating a new transfer mid-flow. The old intent is silently dropped rather than risking it being misread as a PIN or resumed with stale details. | `user_changed_subject` | Not a failure the payer is told about as such — the prefix is folded into whatever the agent replies to their actual message next. If you see `reason=user_changed_subject` in the audit log, this is why: the payer's own message triggered it, not a bug. |
| `❌ PINTWIN galat hai. N koshish baaki — PINTWIN dobara boliye.` | eko rejected the PIN itself — see `_is_pin_failure`: HTTP 401/403, or the message/status text contains "pintwin"/"invalid pin"/"incorrect pin"/"wrong pin". | *(none — only exhaustion is audited, see below)* | Counted against `max_pin_attempts` (default 3). |
| `❌ PINTWIN N baar galat. Transfer cancel ho gaya.` | PIN attempts exhausted. | `pin_attempts_exhausted` | Payer must start the transfer over from scratch. |
| `⏳ Transfer bheja ja chuka hai, par abhi confirm nahi hua.` | eko accepted the request but `tx_status` isn't settled/failed yet — genuinely unknown at this instant. | `pending_at_gateway` | Queued to `SettlementQueue` if possible (see §4); "Settle hote hi bata dunga" appears only if the watch succeeded. |
| `⚠️ <eko's message or 'Transfer nahi ho paya.'>` + "Ye PINTWIN ki galti nahi hai" | The transfer failed for a reason **other than** the PIN — a real eko error surfaces here verbatim (e.g. status 1449, 1297, 1123, "customer_id does not exist in system" — see §3). | `gateway_error` | **The intent stays open** — a retry doesn't require re-dictating payee/amount/name. This is the branch every "customer_id does not exist" / "Invalid Recipient Name" error you've hit so far has come through. |
| `✅ Transaction successful!` (immediate) | eko returned success synchronously (`tx_status` already settled) in `confirm_and_execute`. | *(none — success isn't a "failed"/"cancelled" audit event; see `executed()`)* | Rare — most real transfers land as "pending" first and settle via the poller. |

## 3. eko's own API — confirmed error codes and messages

These are real eko responses, logged verbatim (`eko transfer failed intent=... status=... message=...`) and surfaced to the payer through the "not a PIN" branch above. Everything below has actually been hit in production against this bot.

| Status / message | Meaning | Root cause & fix |
|---|---|---|
| `status: 1449`, `"Invalid Recipient Name"` | `recipient_name` reached eko empty. | Fixed by `_validated_recipient_name` requiring a name before `prepare_transfer` ever runs. |
| `status: 1297`, `"This user does not exist"` | `user_code` sent as empty/wrong for a real, registered `initiator_id`. | `user_code` must match the *payer's own* `initiator_id` — see `_user_code_for` / `EKO_USER_CODES`. An unmapped payer now raises before ever reaching eko. |
| `status: 1123` | Undocumented by eko; only known from one real failure — `message` is the only clue eko gives. | Logged in full now (`_read_transfer_response` logs `status`/`message` together on every non-PIN failure) — check the log line for what eko actually said. |
| `"customer_id does not exist in system"` | `customer_id` was sent as something other than the fixed merchant value — **hit for real** when an earlier version tried sending a mobile-only payee's own number as `customer_id`. | `customer_id` is **never** a payee value, VPA or not — it's merchant identity, paired with `user_code`. Fixed: a bare-mobile payee now gets a synthetic `<mobile>@upi` VPA and rides the normal `customer_vpa` path instead (`agent/payment_tools.py::_resolve_payee_input`). |
| `"Invalid Booklet Number"` | Wrong `initiator_id` shape/value sent for the payer. | `initiator_id` is the payer's own 10-digit mobile (`_initiator_id_for`), never a fixed config value — already fixed. |
| HTTP 402, `"Auth parameters missing"` | One or more of `developer_key`/`secret_key`/`secret_key_timestamp` headers were **absent from the wire**, not just empty. | eko wants these headers *present with an empty value*, not omitted. Curl's `-H "Header: "` silently drops an empty header — use `-H "Header;"` instead (`_curl_get`). If you see this on a *new* endpoint, check the header-building code for that call. |
| HTTP 401 (transfer/verify endpoints) | Real auth failure (wrong/expired keys), or occasionally a `requests`-vs-curl TLS fingerprinting quirk on eko's *sandbox* inquiry host specifically. | Only the sandbox inquiry host (`staging.eko.in`) has shown the fingerprinting issue; the live host (`api.eko.in`) has not. |
| Amount not a whole rupee (`_rupees` raises) | Paise value isn't a multiple of 100. | eko takes whole rupees only — `prepare_transfer` will refuse a fractional-rupee amount before it ever reaches eko. |
| `"eko returned success with no '<field>'"` | `status` said success but eko's reference field (`data.tid` by default) was empty/missing. | Raised as `PaymentError`, not shown to the payer as a normal failure — a success with no reference can't be tracked, so it's treated as a hard error. |

## 4. Settlement / Transaction Inquiry — `agent/settlement.py`

The background poller's outcomes, sent to the payer **separately**, after
the conversation has moved on (see `docs/voice-payments.md`'s "audit trail"
section for why it's a standalone message, not a reply).

| Message | Meaning |
|---|---|
| `✅ Transaction successful! ... Ref: <id>` | `inquire()` came back with `tx_status` in `settled_tx_statuses`. Replaces the earlier "not confirmed yet" message. |
| `❌ <amount> → <payee> nahi ja paya. Paisa wapas aa jayega.` | `tx_status` in `failed_tx_statuses` — money did not move, or moved and reversed. |
| *(nothing — still polling)* | `tx_status` is neither settled nor failed yet, and the settlement is younger than `give_up_after_s`. Polled again on the next sweep. |
| `⚠️ ... ka status N ghante baad bhi confirm nahi hua. ... Ab main track nahi kar paunga` | Backstop only — `give_up_after_s` elapsed with no answer from eko. Does **not** mean the transfer failed; it means eko never said either way. Check the bank statement directly. |
| Log: `Settlement inquiry failed ref=... — will retry` | `gateway.inquire()` raised (network error, HTTP failure, curl error). Treated as "keep waiting", not "failed" — one bad inquiry call must not report a false outcome. |
| Log: `eko inquiry ref=... top_status=69` or `"...not.found..."` | eko has no record of this `tid` yet (or on this host) — **not** the same as failed. Treated as still-pending, because a "we don't have this yet" is often just replication lag, not a real failure. |

**Why status takes 30s–60s to arrive at all**: the poller sweeps every
`PAYMENT_SETTLEMENT_POLL_INTERVAL_S` (default 60s), and its *first* sweep is
delayed by one full interval on purpose — asking eko immediately after a
transfer just went pending wastes an inquiry call before there's been any
time to settle. Lower `PAYMENT_SETTLEMENT_POLL_INTERVAL_S` in `.env` for
faster updates; eko's own inquiry rate limit (25 req/s) is the ceiling on
how low this can safely go under real concurrent load.

## 5. Startup / configuration — never reaches a payer

Raised once, at process start, if `PAYMENT_GATEWAY=eko` but required
settings are missing (`EkoPaymentGateway.__init__` → `EkoSettings.missing_fields`):

| Missing field | Why it's required |
|---|---|
| `EKO_BASE_URL` | No host to send the transfer request to. |
| `EKO_LATLONG` | eko rejects a transfer with no location. |

Everything else (`EKO_DEVELOPER_KEY`, `EKO_AUTH_KEY`, `EKO_USER_CODE`,
`EKO_CUSTOMER_ID`, …) is deliberately allowed to be empty — see the
docstring on `missing_fields` for why each one is safe to omit and what
sending it empty actually does on the wire.

## 6. What is *not* an error

- `PIN galat hai.` reached only via `_is_pin_failure` — a wrong PIN is
  reported plainly, without echoing back any detail about the PIN itself.
- A message that is nothing but digits, with no transfer pending, is never
  forwarded to the model or logged — this is a safety rule
  (`docs/voice-payments.md`), not a failure path.
- `⚠️ TEST MODE — koi asli paisa transfer nahi hua.` on every receipt from
  `StubPaymentGateway` — confirms you're on the fake gateway, not eko.
