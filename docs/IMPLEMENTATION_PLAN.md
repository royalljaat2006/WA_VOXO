# WhatsApp Voice Agent — Implementation Plan

**Status:** All six phases code-complete · STT decided · Docker verified · WhatsApp/eko not yet verified
**Blocked on:** Meta credentials (P1–3), eko API access (P4)
**Last updated:** 2026-07-29

---

## 0. Where this actually stands

All six phases are code-complete: 314 tests, ruff clean, every phase merged to
`main`. Two things that were previously open have since been closed out:

- **STT engine decided.** Benchmarked Whisper vs Sarvam on real audio (9 clips,
  English/Hindi/Hinglish) — Sarvam won on both latency (~15x faster) and
  Hindi/Hinglish accuracy. `STT_ENGINE=sarvam` is now the configured default.
  Details: [phase-0-stt-benchmark.md § Results](phase-0-stt-benchmark.md#results).
- **Docker image built and run.** `docker build` succeeds, the container starts
  as non-root, `/health` responds correctly, and Docker's own `HEALTHCHECK`
  reports `healthy`. Not yet deployed anywhere — verified locally only.

**What's still unverified: the WhatsApp round trip itself**, because that needs
Meta credentials, and **Phase 4 on real numbers**, because that needs eko API
access. Neither is a code problem.

| Blocker | Unblocks | Who |
|---|---|---|
| Meta app + access token (~15 min) | Phases 1, 2, 3, 5 end to end, against real WhatsApp | Vishesh |
| eko internal API access | Phase 4 on real data (stub works today) | eko platform team |

The highest-value next step is still the Meta credentials: one real voice note
verifies Phases 1, 2, 3 and 5 together.

One caveat worth repeating because it is easy to forget:

- **Phase 4 answers with fake numbers.** `StubCampaignSource` is deterministic
  demo data. The server warns at startup and `/health` reports `stub_data: true`.

---

## 1. What we are building

A user sends a **voice note** to a WhatsApp Business number and gets an answer back
from eko's internal systems.

```
"aaj kitne campaign gaye?"     →  "Aaj 12 campaigns gaye. 8 delivered, 3 pending, 1 failed."
"kal ka delivery report bhej"  →  [formatted report]
```

The value is that the user never has to open a dashboard, log in, or type. They talk,
in the Hinglish they actually speak, and get the number they wanted.

### Why voice, why WhatsApp

Banks and fintechs already run official WhatsApp numbers where customers *type*.
Nothing technically prevents those numbers from accepting voice — the Cloud API
delivers audio just like it delivers text. The gap is product, not platform.

---

## 2. Scope

### In scope
- Voice notes sent **to our business number**
- Hinglish, Hindi and English speech
- Read-only queries against existing internal APIs (campaign counts, delivery reports)
- Text replies

### Explicitly NOT in scope

| Not doing | Why |
|---|---|
| Live transcript while the mic is held | Cloud API only sees a message after it is sent. No API exposes in-progress recording. |
| Reading the user's personal WhatsApp chats | Cloud API is scoped to our own business number. Intercepting the real app needs an Android accessibility hack (fragile) and is impossible on iOS. |
| Voice replies (TTS) | Text replies are cheaper, skimmable, and copy-pasteable. Revisit after v1. |
| Any transactional action (transfers, refunds, sending campaigns) | Voice is not strong enough authentication for a state-changing operation. See §7. |

> ⚠️ **Demo discipline:** never imply live-while-recording transcription. Set the
> expectation as "send a voice note, get an answer" from the first demo onward.

---

## 3. Architecture

```
 WhatsApp user
      │  voice note
      ▼
 Meta Cloud API ──webhook──▶  FastAPI server
                                   │
                                   │ 1. verify signature
                                   │ 2. download media (media_id → URL → bytes)
                                   ▼
                             audio.to_wav()          ← shared module
                                   │
                                   ▼
                             Transcriber             ← shared interface
                             (whisper | sarvam | …)
                                   │  transcript
                                   ▼
                             Agent (LLM + tools)
                                   │  tool call
                                   ▼
                             eko internal APIs
                                   │  data
                                   ▼
                             Cloud API send message ──▶ user
```

### Key design decision — one STT interface

`Transcriber` (in `src/whatsapp_voice/stt/base.py`) is the seam that makes Phase 0's
benchmark and Phase 3's production pipeline share the same engine code. The benchmark
runs *all* configured engines; production runs *one*. Neither duplicates the other.

Changing engines after the benchmark is a `.env` change, not a code change.

### Repo layout

```
src/whatsapp_voice/     # importable package — everything reusable lives here
  config.py             # typed settings, one place per phase
  audio.py              # opus → 16kHz mono wav (shared by benchmark + pipeline)
  metrics.py            # WER and text normalisation
  stt/                  # engines behind one interface
    base.py             #   Transcriber ABC, Transcript, TranscriptionError
    registry.py         #   config → the engines we can actually run
    local_whisper.py openai_stt.py sarvam.py elevenlabs.py
  whatsapp/             # Phases 1–2 — Cloud API + webhook server
    client.py           #   send messages, download media
    app.py              #   FastAPI webhook (verify, receive, dedup, dispatch)
    payloads.py         #   Meta's JSON → typed messages
    signature.py        #   X-Hub-Signature-256 verification
    handlers.py         #   MessageHandler seam: echo → transcribe → agent
  agent/                # Phase 4 — LLM, tool definitions, conversation state
  auth/                 # Phase 5 — caller authorisation

tools/                  # operator scripts, not imported by the app
  benchmark_stt.py      # Phase 0 harness

tests/                  # pytest
data/                   # gitignored — audio, results, ground truth
docs/                   # this plan + per-phase notes
```

**Rule:** anything two phases both need goes in `src/whatsapp_voice/`. `tools/` may
import from `src/`; `src/` never imports from `tools/`.

---

## 4. Branching model

Trunk-based, one branch per phase.

- `main` — always working. Every merge to `main` is a phase that met its acceptance criteria.
- `phase/N-short-name` — the working branch for one phase.

```bash
git switch -c phase/1-meta-setup     # start a phase
# ... commits ...
git switch main && git merge --no-ff phase/1-meta-setup
```

`--no-ff` keeps each phase visible as a unit in the history.

Commits are conventional-ish and scoped: `feat(stt): add sarvam engine`,
`docs: record phase 0 results`, `test(audio): cover corrupt input`.

---

## 5. Phases

Each phase merges to `main` only when its acceptance criteria pass.
**Ask before starting a new phase.**

### Phase 0 — STT benchmark ⏳ IN PROGRESS

Prove that *some* engine hears Hinglish plus our campaign jargon well enough to build on.
Deliberately first: if this fails, no amount of agent quality rescues the product.

| | |
|---|---|
| **Deliverables** | `tools/benchmark_stt.py`, `stt/` package, tests, this plan |
| **Acceptance** | 10–15 real voice notes benchmarked across ≥2 engines; one engine chosen and recorded in §8 |
| **Blocked on** | Vishesh recording real voice notes into `data/audio/` |

Run it:
```bash
.venv/bin/python tools/benchmark_stt.py
```
See [phase-0-stt-benchmark.md](phase-0-stt-benchmark.md) for what to record and how to read results.

---

### Phase 1 — Meta setup ⏳ CODE BUILT, awaiting credentials

| | |
|---|---|
| **Goal** | Working Cloud API credentials |
| **Deliverables** | ✅ `WhatsAppSettings` config, ✅ `WhatsAppClient` (send/media, reused by Phases 2–4), ✅ `tools/send_hello.py`, ✅ [setup runbook](phase-1-meta-setup.md), ✅ tests. **Manual (Vishesh):** Meta app, test number, token → `.env`. |
| **Acceptance** | `python tools/send_hello.py <number>` lands a message on Vishesh's phone |
| **Blocked on** | Vishesh completing the Meta steps in [phase-1-meta-setup.md](phase-1-meta-setup.md) |
| **Watch out** | Test numbers only message pre-registered recipients. Temporary tokens expire in ~24h — use a System User token for the demo. Production needs business verification (1–2 weeks). |

---

### Phase 2 — Webhook server ⏳ CODE BUILT, awaiting Meta config

| | |
|---|---|
| **Goal** | Two-way text messaging |
| **Deliverables** | ✅ FastAPI app (`GET` verification, signed `POST` receiver, background dispatch, dedup), ✅ payload parser (typed text/audio/unsupported — reused by Phase 3), ✅ swappable `MessageHandler` + `EchoHandler`, ✅ `tools/run_server.py`, ✅ [runbook](phase-2-webhook.md), ✅ tests. **Manual (Vishesh):** ngrok + Meta webhook config. |
| **Acceptance** | Text the number → server logs it → echo reply arrives |
| **Blocked on** | Phase 1 credentials, then the steps in [phase-2-webhook.md](phase-2-webhook.md) |
| **Watch out** | Subscribing to the **`messages`** webhook field is a separate click from verifying — miss it and verification passes while no messages ever arrive. Free ngrok URLs change on restart. |

---

### Phase 3 — Voice pipeline ⏳ CODE BUILT, awaiting credentials

| | |
|---|---|
| **Goal** | Voice note in, transcript out |
| **Deliverables** | ✅ `TranscribeHandler` (fetch → convert → transcribe → reply), ✅ `HANDLER=transcribe` switch, ✅ engine warmup at startup, ✅ [runbook](phase-3-voice.md), ✅ 16 tests covering every failure path |
| **Acceptance** | Send a voice note → correct transcript replies |
| **Blocked on** | Meta credentials (Phase 1) to test end to end; engine choice from Phase 0 |
| **Watch out** | The media URL expires in ~5 minutes and needs the bearer token to fetch — `fetch_media` resolves and downloads back-to-back for this reason. |

This is the first genuine demo moment. Built entirely from existing pieces — no new
machinery, just `client.fetch_media` → `audio.to_wav` → `transcriber.transcribe` →
`client.send_text`.

---

### Phase 4 — Agent ✅ CODE BUILT (stub data)

| | |
|---|---|
| **Goal** | Transcript → the right internal API call → a human answer |
| **Deliverables** | Claude integration, internal APIs as tool definitions, conversation state, clarifying questions for ambiguous asks |
| **Acceptance** | "aaj kitne campaign gaye?" returns real data; an ambiguous ask triggers a follow-up question rather than a guess |
| **Watch out** | ASR errors on campaign names propagate here. The agent should confirm rather than fuzzy-match onto the wrong campaign. |
| **Blocked on** | Access + credentials for eko's internal APIs — **start this conversation during Phase 1, it is the likeliest schedule risk** |

---

### Phase 5 — Auth & safety ✅ CODE BUILT

| | |
|---|---|
| **Goal** | Only the right people get data |
| **Deliverables** | Phone-number allowlist, number→role mapping, read-only enforcement, rate limiting, graceful failure ("samajh nahi aaya, dobara boliye") |
| **Acceptance** | An unknown number gets a refusal; a known number gets only its own scope |

---

### Phase 6 — Demo & production ✅ CODE BUILT (image unverified)

| | |
|---|---|
| **Goal** | Runs without a laptop open |
| **Deliverables** | Real hosting (replaces ngrok), structured logging, cost tracking, business verification if a real number is wanted |

---

## 6. Timeline

| Week | Phases |
|---|---|
| 1 | 0, 1, 2 — text echo working end to end |
| 2 | 3, 4 — voice transcription working, agent taking shape |
| 3 | 4, 5 — full demo |

Assumes internal API access lands by end of week 1.

---

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Hinglish ASR accuracy** | Kills the product | Phase 0 exists solely to find this out first, on real audio, before anything is built on top |
| **Internal API access delayed** | Blocks Phase 4 | Raise it in week 1, not week 3. Build Phase 4 against a stub if needed. |
| **Business verification delay** | Blocks a *public* launch only | Demo runs fine on a test number. Start verification early if a real number is wanted. |
| **Voice as authentication** | Data leak | Read-only scope + number allowlist. Anything transactional stays out until there is a real auth story. Numbers cannot be spoofed on WhatsApp, but a phone can be borrowed. |
| **Expectation drift toward live transcription** | Disappointed stakeholders | Stated as an explicit non-goal in §2; repeat it in every demo |

---

## 8. Decision log

| Date | Decision | Reasoning |
|---|---|---|
| 2026-07-28 | Cloud API, not personal-WhatsApp interception | Official, server-side, platform-independent. The hack is Android-only and breaks on WhatsApp updates. |
| 2026-07-28 | Phase 0 (ASR benchmark) before any build | Highest-uncertainty, highest-impact unknown. Cheapest to test in isolation. |
| 2026-07-28 | Engines behind one `Transcriber` interface | Benchmark and production share engine code; swapping engines is config, not a rewrite. |
| 2026-07-28 | Python 3.11 | System default is 3.9, too old for parts of the stack. |
| 2026-07-29 | **STT engine: Sarvam** (`STT_ENGINE=sarvam`) | Benchmarked on real audio: ~15x faster than local Whisper (0.7s vs ~11s avg) and no content errors on Hindi/Hinglish where Whisper had one ("फेल" heard as "खेल"). Latency alone decides it for a chat product. Caps audio at 30s — known gap, see docs/phase-0-stt-benchmark.md. |
| 2026-07-29 | Agent provider: Nemotron Ultra kept as configured despite documented latency variance (3.9–42s) | Explicit call by the repo owner; Super (`nemotron-3-super-120b-a12b`) measured faster and is one `.env` line away if this needs revisiting. |
| _pending_ | Hosting target | Phase 6 — owner has said "rack" for now; not yet mapped to Render/Railway/AWS/self-hosted, needs clarifying |

---

## 9. Cost (demo scale)

| Item | Cost |
|---|---|
| Meta test number | Free |
| WhatsApp service conversations | Free (user-initiated) |
| STT | ~₹0.5–2 per audio minute (local Whisper: ₹0) |
| LLM | ~₹1–5 per query |
| Hosting | Free tier initially |

Well under ₹1,000/month during development.
