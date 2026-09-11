# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The venv is **Python 3.11** — the system default (3.9) is too old for parts of the stack.
Always invoke through `.venv/bin/python`, never a bare `python`.

```bash
# Setup
python3.11 -m venv .venv
.venv/bin/pip install -e ".[stt,server,dev]"
cp .env.example .env

# Tests and lint
.venv/bin/python -m pytest                          # all
.venv/bin/python -m pytest tests/test_metrics.py    # one file
.venv/bin/python -m pytest tests/test_metrics.py::TestWordErrorRate::test_identical_is_zero
.venv/bin/ruff check .
.venv/bin/ruff check --fix .

# Operator scripts (tools/ are run by humans, never imported)
.venv/bin/python tools/benchmark_stt.py             # Phase 0: compare STT engines
.venv/bin/python tools/send_hello.py 91XXXXXXXXXX   # Phase 1: prove credentials work
.venv/bin/python tools/run_server.py --reload       # Phase 2+: webhook server
```

Overriding a setting on the command line works because real env vars beat `.env`:
`WHISPER_MODEL=tiny .venv/bin/python tools/benchmark_stt.py`

## Where to look

| Need | File |
|---|---|
| How the pieces fit, file-by-file map | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| **Step-by-step for a specific change** (add an engine/tool/handler, plug in the real API) and debugging playbooks | [`whatsapp-voice-agent` skill](.claude/skills/whatsapp-voice-agent/SKILL.md) |
| **Anything Meta-side** — tokens, webhooks, error codes, every failure hit so far and its fix | [docs/meta-integration-runbook.md](docs/meta-integration-runbook.md) |
| Voice payments: the PIN gate, amount parsing, safety limits | [docs/voice-payments.md](docs/voice-payments.md) |
| Every payment error message — cause and what to do | [docs/payment-errors.md](docs/payment-errors.md) |
| Current status, phases, decision log | [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) |
| Setup for one phase | `docs/phase-N-*.md` |

## Architecture

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before writing code. The essentials:

**There is no frontend.** WhatsApp is the user interface. Every file here is backend.
This is a decision, not a gap — see ARCHITECTURE.md §7 before proposing a dashboard.

**Four layers, dependencies point downward only:**
transport (`whatsapp/app.py`, `signature.py`, `payloads.py`) → product logic
(`whatsapp/handlers.py`, later `agent/`) → capabilities (`stt/`, `audio.py`,
`whatsapp/client.py`) → foundation (`config.py`, `metrics.py`).

**Two seams carry the whole design.** New work plugs into one of them:

- **`Transcriber`** (`stt/base.py`) — one file per engine. The benchmark runs *all*
  configured engines (`build_transcribers`), production runs *one*
  (`build_primary_transcriber`). Adding an engine means a new file plus one branch in
  `stt/registry.py` — never inline an STT call at a call site. Override `warmup()` for
  anything with expensive init, or its load time is charged to the first request.
- **`MessageHandler`** (`whatsapp/handlers.py`) — one per phase: `EchoHandler` (2),
  `TranscribeHandler` (3), `AgentHandler` (4), `PaymentHandler`
  (`whatsapp/payment_handler.py`). Selected by `HANDLER` in `.env` and
  dispatched in `_build_inner`. **New behaviour means a new handler, never an `if` in
  the webhook route.** The transport layer was written once and should not be reopened.
  **There is no access guard.** `build_handler` returns the handler directly (wrapped
  only in `GroupHandler` when groups are enabled) — every number that reaches this
  server is answered, payments included. The allowlist/role/`+pay` system
  (`auth/`) that used to gate this was removed deliberately; see
  [docs/phase-5-auth.md](docs/phase-5-auth.md) for why and what replaces it.

Two more boundaries worth knowing:

- **`Agent`** (`agent/base.py`) — the WhatsApp layer never imports an LLM SDK. Three
  providers, chosen by `AGENT_PROVIDER`: `ClaudeAgent`, plus `NemotronAgent` (NVIDIA
  NIM) and `GeminiAgent` (Google), which are four-line subclasses of
  `OpenAICompatibleAgent` — it holds the tool loop, they answer only "which key,
  which URL, which model". Claude is separate, not one class with a flag, because
  its wire format genuinely differs (tool-arg parsing, tool-result shape,
  `temperature` vs `effort`). All reuse the same prompt, tools, and conversation
  store. **Gemini's free tier lets Google read the traffic** — payee numbers, UPI
  IDs and amounts — so it is a development/demo option, not a default for real
  transfers; see `agent/gemini_agent.py`.
- **`CampaignDataSource`** (`agent/datasource.py`) — **the file to change when eko
  API access arrives.** Currently backed by `StubCampaignSource`, which returns
  deterministic fake data. It is deliberately read-only: no `send`/`create`/`update`
  method exists, because phone-number identity cannot authorise a state change.
- **`PaymentGateway`** (`agent/payments.py`) — protocol plus `StubPaymentGateway`
  (moves no money) and `EkoPaymentGateway` (eko's ICICI transfer API; `pintwin` is
  the PIN). See [docs/voice-payments.md](docs/voice-payments.md).
- **`HandoffKind`** (`agent/handoff.py`) — how the payer authorises. `spoken_pin`
  is the POC default and is **not** RBI-compliant as a second factor; the others
  hand the credential to the payer's own app and require the signed, idempotent
  `POST /payments/callback`.

## Load-bearing constraints

These are not style preferences — breaking them causes real failures:

- **The webhook must return 200 immediately**, before any slow work. Meta retries
  anything that isn't a prompt 200, so a slow reply turns one voice note into a pile of
  duplicates. Handlers run in a background task.
- **Media URLs expire in ~5 minutes** and need the bearer token. `client.fetch_media`
  resolves and downloads back-to-back for this reason — never queue a resolved URL.
- **Verify `X-Hub-Signature-256` against the raw request body.** Re-serialised JSON
  produces a different MAC and fails.
- **Downloaded audio is customer data.** Write it to a temp dir removed in a `finally`;
  `data/` is gitignored for the same reason.
- **Every handler failure path must reply with something.** A user cannot tell a broken
  bot from a slow one.
- **Don't rank STT engines by WER on Hinglish.** One engine writes "aaj", another "आज" —
  same meaning, WER 100%. `tests/test_metrics.py` documents this trap. Read transcripts.
- **Never strip punctuation with `[^\w\s]`.** `\w` excludes Unicode category Mn, so
  the regex eats Devanagari combining marks — "हज़ार" loses its nukta, "पाँच" its
  chandrabindu, and every Hindi input silently stops matching. Strip by category
  instead (`unicodedata.category(c).startswith(("P", "S"))`). This has bitten this
  project three times: `metrics.py`, then `parsing.py` twice.
- **Campaign name lookup is exact-match, deliberately not fuzzy.** ASR mangles names;
  a near match would produce a confidently wrong report, which is worse than an error.
  The same rule applies to **payee** names, where the cost is money, not a bad number.
- **A PIN never reaches the model, the conversation history, or the logs.** The gate
  (`agent/payment_flow.py`) runs *in front of* the agent, not behind it, and the
  transcript is withheld from the log for the whole pending-payment window — by the
  time you know it was a PIN it has already been written down.
- **There is no `execute_transfer` tool, and there must never be one.** The most a
  model can do is `prepare_transfer`, which returns an unauthorised intent. Execution
  needs a PIN, which only the handler has. This is what makes a prompt injected into
  a transcript unable to move money; `tests/test_payments.py::TestNoExecutePath`
  fails if it is removed.
- **Parse amounts in `agent/parsing.py`, never in the model.** Hindi `do` is both
  "2" and "give", so "Ramesh ko 2000 bhej do" reads as two amounts unless a spoken
  number word requires a multiplier after it. Anything ambiguous returns `None` and
  the bot asks — an extra question costs a second, a wrong amount costs money.
- **Money is integer paise, never float rupees.** Floats lose precision on money.
- **A message that is nothing but digits is never forwarded to the model**, even with
  no transfer pending. Making that conditional on our own state being correct is what
  leaks: a model that says "PIN bolo" without having called `prepare_transfer` leaves
  nothing pending, and the user's PIN goes straight into its context. Observed in a
  real run — `tests/test_payment_flow.py::TestNoPendingIntent` pins it.
- **Payments never complete in a group.** A PIN spoken as a voice note there is
  audible to every participant and stays in the chat to be replayed, so restricting
  *who* may pay does not help — it is the payer's own PIN being exposed.
  `GroupHandler` redirects payment requests to a 1:1 chat; reporting still works.
- **Reply to `message.reply_to`, never `message.from_number`.** In a group they
  differ, and answering the sender turns a group answer into a surprise private
  message.
- **A number from a shared contact card is not the same as a dictated one.** WhatsApp
  normalised and verified it, so it skips the digit read-back. `PayeeSource` carries
  that distinction; losing it either drops the safety step or nags about a verified
  number.
- **Tool results go back in ONE user message** on Anthropic. NVIDIA is the opposite —
  one `tool` message per call. Using the wrong shape silently loses results.
- **The system prompt must carry today's date** (`system_prompt_for`). Without it a
  model guesses from training data, every dated tool call falls outside range, and
  the tool-turn budget burns out. This has happened.
- **Never trust the model to format for WhatsApp.** `agent/formatting.py` rewrites
  markdown on every reply — prompting alone was observed to leak `**bold**`.
- **More than one worker needs `REDIS_URL`.** Deduplication and pending transfers
  live in `store.py`; without Redis they are per-process, so N workers means
  duplicate replies and a spoken PIN that finds no transfer waiting.
  `guard_worker_count` refuses that combination at startup — do not remove it,
  the silent version is very hard to diagnose.
- **Never write `x = arg or Default()` for a collaborator that defines `__len__`.**
  An empty `MemoryStore` is falsy, so `store or MemoryStore()` silently discarded the
  shared store and gave every worker its own. Use `arg if arg is not None else …`.

## Configuration

Every setting is a typed field in `config.py`. No `os.environ` reads anywhere else.
Each phase adds its own dataclass section. `.env` is gitignored; `.env.example`
documents the shape with empty values.

## Current state

All six phases are code-complete. STT engine is decided (`STT_ENGINE=sarvam`, from
a real benchmark) and the Docker image builds and runs cleanly (verified locally —
non-root, `/health` correct, `HEALTHCHECK` passes).

Voice payments are built on top (`HANDLER=payment`) and verified end to end
locally: text and voice, English and Devanagari, correct PIN, wrong PIN, cancel,
caps, and a prompt-injection attempt. See [docs/voice-payments.md](docs/voice-payments.md).

**What's still unverified is the WhatsApp round trip and anything on real
numbers** — blocked on Meta credentials, eko API access and eko payment API
access. Keep in mind before claiming anything works: the agent answers with
**fake numbers** (`StubCampaignSource`) and **moves no money**
(`StubPaymentGateway`) until that access lands.

## Workflow

The project runs in six phases — see [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)
for status, acceptance criteria, and the decision log. Each phase has a runbook in `docs/`.

- **Ask before starting a new phase.** This is a standing instruction from the repo owner.
- One branch per phase: `phase/N-short-name`, merged to `main` with `--no-ff` so each
  phase stays one visible unit of history. `main` always works.
- A phase merges only when its acceptance criteria pass.
- Phases are additive. If a phase seems to require editing the transport layer, that is a
  signal the design was wrong — stop and reconsider rather than patching.

## Tests

`tests/test_<module>.py` mirrors `src/…/<module>.py`, so a failure names the file to open.
**No network in tests** — mock the transport (`tests/test_whatsapp_client.py` shows the
pattern). Cover the failure paths, not just the happy one: expired tokens, malformed
payloads, duplicate deliveries, unconvertible audio.
