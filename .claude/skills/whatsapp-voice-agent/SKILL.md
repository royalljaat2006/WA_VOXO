---
name: whatsapp-voice-agent
description: Recipes for extending the WhatsApp_Voice repo — a WhatsApp voice-note agent for eko. Use when adding a speech-to-text engine, adding an agent tool, plugging a real data source in behind CampaignDataSource, adding a MessageHandler or bot behaviour, changing the system prompt, touching auth or rate limits, debugging why a voice note produced no reply or a duplicate reply, or preparing a demo or deployment of this project.
---

# Working on the WhatsApp Voice Agent

Task recipes for this repo. For the facts and constraints that apply to everything
here, `CLAUDE.md` is loaded automatically. For architecture, read
`docs/ARCHITECTURE.md`. This file is the "how do I do X" layer.

## Before you start

```bash
.venv/bin/python -m pytest && .venv/bin/ruff check .
```

Both must pass before and after your change. The venv is Python 3.11 — never invoke
a bare `python`.

**Two things are easy to forget and change what you should claim:** the agent
currently answers with **fake numbers** (`StubCampaignSource`), and the **Docker
image has never been built**. Don't describe either as working.

---

## Recipe: add a speech-to-text engine

1. New file `src/whatsapp_voice/stt/<vendor>.py`, subclassing `Transcriber`.
   Implement `_run(audio) -> (text, language)` only — `base.py` handles timing and
   error wrapping. Raise `TranscriptionError` for expected failures.
2. Override `warmup()` if setup is expensive (model load, client construction).
   **Skipping this charges the load time to the first user's reply.**
3. Add credentials to `SttSettings` in `config.py`, and to `.env.example`.
4. Add one branch in `stt/registry.py::build_transcribers`, guarded on the key
   being present — a missing key must skip the engine, not crash.
5. Test in `tests/test_stt_registry.py`. No network: assert the engine appears when
   its key is set and is absent when it isn't.

**Never inline an STT call at a call site.** One engine per file is what keeps each
one ~35 lines.

## Recipe: add a tool the agent can call

1. Subclass `Tool` in `src/whatsapp_voice/agent/campaign_tools.py` (or a new
   `<domain>_tools.py` if it is a different domain).
2. Write the `description` to say **when to call it**, not just what it does — that
   is the main lever on whether the model reaches for it. Include example phrasings
   in the user's own words ("aaj kitne campaign gaye?").
3. `input_schema` needs `additionalProperties: false` and a `required` list.
4. `run()` returns **compact facts, not prose** (`sent=2840` style). The model writes
   the user-facing sentence, in whatever language they spoke. Raise `ToolError` with
   a message written for a reader.
5. Register it in the `campaign_tools(source)` list, or a parallel factory.
6. Test dispatch and the error paths in `tests/test_agent_tools.py`.

**Do not add fuzzy matching.** Lookups are exact-match on purpose: ASR mangles
names, and a near match produces a confidently wrong answer — worse than an error.
A miss should return the valid options so the agent can ask.

## Recipe: plug in eko's real campaign API

This is the change the stub exists to make cheap. One file.

1. In `src/whatsapp_voice/agent/datasource.py`, add a class implementing
   `CampaignDataSource` — three read methods. Raise `DataSourceError` for unknown
   campaigns, missing data, or upstream failures.
2. Add a branch in `agent/factory.py::build_data_source`.
3. Set `AGENT_DATA_SOURCE` to the new name.
4. If the real API's shapes disagree with `CampaignCount` / `DeliveryReport` (they
   probably will), change those dataclasses and the three tools in
   `campaign_tools.py`. **Nothing above the data layer moves.**

**Keep it read-only.** Do not add a `send_campaign` or any mutating method. Callers
are authenticated by phone number, which cannot authorise a state change — the
absence of write methods is what makes that unreachable by a prompt. See
`docs/phase-5-auth.md` for the threat model.

## Recipe: change what the bot does

New behaviour is a **new `MessageHandler`**, never an `if` in the webhook route.

1. Subclass `MessageHandler` in `whatsapp/handlers.py`. If you need the
   fetch → convert → transcribe front half, subclass `TranscribeHandler` and
   override `_on_transcript` instead — that is what `AgentHandler` does.
2. Add a branch in `app.py::_build_inner` and a `HANDLER` value.
3. Document the new value in `.env.example` and in `tools/run_server.py`'s startup
   banner.
4. **Every failure path must reply with something.** A user cannot tell a broken bot
   from a slow one. Follow the pattern in `TranscribeHandler._handle_audio`.
5. `build_handler` wraps your handler in `GuardedHandler` automatically — do not add
   your own auth check.

## Recipe: tune the system prompt

It lives in `agent/claude_agent.py::SYSTEM_PROMPT`. Three instructions there are
load-bearing; understand why before editing:

- **Answer in the user's language mix** — without it, Hinglish in gets formal
  English out, which reads as a different product.
- **Never state a figure a tool did not give you** — a confidently wrong number is
  the expensive failure.
- **Confirm misheard names rather than guessing** — paired with the exact-match
  data layer.

`tests/test_claude_agent.py::TestRequestShape` asserts the prompt reaches the
request. After any change, re-read it as if you were the model: is each instruction
actionable, or just reassuring?

## Recipe: touch auth or rate limits

- Allowlist and roles: `auth/policy.py`. Rate limiting: `auth/rate_limit.py`. The
  gate itself: `auth/guard.py`.
- **Keep identity checked before rate.** Reversing them lets unknown numbers consume
  limiter memory, and a distinct "too many requests" reply tells a prober their
  number is recognised.
- **Keep the unknown-number reply uninformative.** It must not name eko, the
  allowlist, or what the service does. There is a test asserting this.
- **Do not count rejected attempts** against the window — that turns a limit into a
  permanent lockout.
- Adding a role: extend `Role` and `_ROLE_RANK`. Gate a sensitive tool with
  `caller.may(Role.ANALYST)`.

---

## Debugging: a voice note produced no reply

Work down this list — the order matches how often each is the cause.

| Check | How |
|---|---|
| Was the sender allowlisted? | Look for `Denied unknown number …NNNN` in the logs |
| Rate limited? | `Rate limited …NNNN` |
| Did Meta deliver at all? | `Queued AudioMessage from …` should appear |
| Signature rejected? | `Rejected webhook with invalid signature` — `WHATSAPP_APP_SECRET` mismatch |
| Media download failed? | Media URLs expire in ~5 min; `fetch_media` must resolve and download back-to-back |
| Transcription empty? | `Transcribed … in Xs:` with nothing after it |
| Which handler is running? | `curl <host>/health` reports it |

## Debugging: the user got two replies to one voice note

Deduplication is per-process (`SeenMessages` in `app.py`). **You are running more
than one worker.** The Dockerfile pins `--workers 1` for exactly this reason.
Conversation history and rate limits break the same way. Fixing it means shared
state (Redis), not a higher worker count — `docs/phase-6-deploy.md` has the table.

## Debugging: the agent answers without calling a tool

- Raise `AGENT_EFFORT` (`medium` → `high`). Lower effort scopes work more tightly.
- Make the tool's `description` more prescriptive about *when* to call it.
- Confirm the tool is registered: the log line lists `tools=[…]` per answer.

---

## Preparing a demo

1. `curl <host>/health` — confirm `auth_enabled: true`,
   `signature_verification: true`, and check `stub_data`.
2. If `stub_data: true`, **say so on the slide.** Those numbers are invented, and
   the server prints a warning at startup for the same reason.
3. Use a **permanent** System User token, not a 24-hour temporary one
   (`docs/phase-1-meta-setup.md`).
4. Record the demo phrasings in advance and check the transcripts — ASR on campaign
   names is the most likely thing to embarrass a live demo.
5. Never imply live-while-recording transcription. The Cloud API only sees a voice
   note **after** it is sent; that is an explicit non-goal in the plan.

## Adding a phase or a large piece of work

- Branch `phase/N-short-name`, merge to `main` with `--no-ff`.
- **Ask the repo owner before starting a new phase** — a standing instruction.
- Work should be **additive**. If it seems to require editing the transport layer
  (`app.py`, `signature.py`, `payloads.py`), stop: that is a signal the design is
  wrong, not something to patch around. The webhook route has not been reopened
  since Phase 2 and should stay that way.
- Every phase gets a runbook in `docs/` that someone can follow without having read
  the others, and it should state its own known limits honestly.
