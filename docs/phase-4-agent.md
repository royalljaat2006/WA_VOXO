# Phase 4 — The Agent

**Goal:** a spoken question gets a real answer.

```
🎤 "aaj kitne campaign gaye?"   →   "Aaj 9 campaigns gaye — AEPS Onboarding 2,
                                     Merchant Referral 3, DMT Winback 4."
```

**Acceptance:** a voice note asking about campaigns comes back with data the agent
actually fetched, and an ambiguous question produces a clarifying question rather
than a guess.

---

## ⚠️ Read this first: the numbers are fake

There is **no eko API access yet**, so the tools are backed by
`StubCampaignSource` — deterministic fake data. Every figure the agent reports
today is invented.

This was a deliberate choice over waiting: the whole pipeline can be built,
tested, and demonstrated now, and swapping in the real API later touches exactly
one file. But it means:

- **Do not show stub figures to a stakeholder as real data.** The server prints a
  warning at startup for this reason.
- The stub's shapes are a *guess* at eko's API. They will probably be wrong. That
  is fine — see [Plugging in the real API](#plugging-in-the-real-api).

---

## What was built

| Piece | File | Does |
|---|---|---|
| Agent seam | `agent/base.py` | `Agent` interface + `AgentReply`. The WhatsApp layer never imports an LLM SDK |
| Claude agent | `agent/claude_agent.py` | The tool-calling loop, system prompt, refusal handling |
| Tool interface | `agent/tools.py` | `Tool` + `ToolRegistry` — dispatch, error containment |
| Campaign tools | `agent/campaign_tools.py` | `get_campaign_count`, `get_delivery_report`, `list_campaigns` |
| Data boundary | `agent/datasource.py` | `CampaignDataSource` protocol + the stub |
| Conversation state | `agent/conversation.py` | History so "aur kal?" resolves |
| Assembly | `agent/factory.py` | Settings → a working agent |
| Handler | `whatsapp/handlers.py` | `AgentHandler`, extending `TranscribeHandler` |

---

## Setup

```bash
.venv/bin/pip install -e ".[stt,server,agent,dev]"
```

In `.env`:
```
HANDLER=agent
ANTHROPIC_API_KEY=sk-ant-...
```

Then:
```bash
.venv/bin/python tools/run_server.py --reload
```

Send a voice note asking *"aaj kitne campaign gaye?"* The reply shows the
transcript it heard, then the answer.

---

## Providers

Three are supported, chosen with `AGENT_PROVIDER`:

| | `claude` (default) | `nemotron` | `gemini` |
|---|---|---|---|
| API | Anthropic Messages | NVIDIA NIM (OpenAI-compatible) | Google, OpenAI-compatible endpoint |
| Default model | `claude-sonnet-5` | `nvidia/llama-3.3-nemotron-super-49b-v1.5` | `gemini-3.6-flash` |
| Key | `ANTHROPIC_API_KEY` | `NVIDIA_API_KEY` | `GEMINI_API_KEY` |
| Depth control | `AGENT_EFFORT` | `AGENT_TEMPERATURE` | `AGENT_TEMPERATURE` |
| Free tier | no | yes | yes — **but see the warning below** |

`claude` is its own implementation because Anthropic's wire format genuinely
differs — see the table at the top of `openai_compatible.py`. The other two are
four-line subclasses of `OpenAICompatibleAgent`, which holds the whole tool
loop: Google publishes an OpenAI-dialect endpoint, so the same `openai` client
drives both and no second SDK is needed. Each subclass answers only three
questions — which key, which URL, which model.

All three reuse the same system prompt, tools, and conversation store.

Note the settings do **not** overlap: Claude rejects `temperature`, and neither
OpenAI-compatible provider has `effort`. Sharing one field would mean sending a
parameter that 400s on whichever provider was not selected. The same goes for
keys — `is_configured` checks only the *selected* provider's key, so a
`GEMINI_API_KEY` left over from a trial cannot make an unconfigured Claude
deployment look ready.

### ⚠️ Gemini's free tier reads your traffic

On the **unpaid** Gemini API, Google uses submitted content and generated
responses to improve its products, and human reviewers may read and annotate
them. Google's own guidance is not to send personal or confidential data to the
unpaid service. The paid tier and Vertex AI do not do this. (EEA/Switzerland/UK
get the paid data terms even on the free tier; India does not.)

On this deployment, what reaches the model is **payee phone numbers, UPI IDs,
transfer amounts and payee names** — real, identifiable people.

What it never sees, on any provider, is **the PIN**: the gate in
`payment_flow.py` runs in front of the agent, not behind it. That is what makes
a free-tier trial merely questionable rather than immediately disqualifying.

`build_agent` logs a warning at every start when `gemini` is paired with a real
payment gateway. Fine for development, wiring and fake-number demos; read
<https://ai.google.dev/gemini-api/terms> before leaving it on for real
transfers or a compliance review.

```
AGENT_PROVIDER=nemotron
NVIDIA_API_KEY=nvapi-...
```

### What was actually observed on Nemotron 3 Ultra

Tested live against the real API, not assumed:

- **Tool calling works correctly.** Picks the right tool, resolves "aaj" and "kal"
  to ISO dates, chains `list_campaigns` → `get_delivery_report` when a name needs
  resolving.
- **Hinglish output is good** — replies in the user's register without prompting
  beyond the shared system prompt.
- **It is looser about confirming misheard names.** Given "Diwaali Cashbak" it
  looked up the real list and then answered for "Diwali Cashback" *without saying
  it had substituted*. The prompt asks it to confirm first. The substitution
  happened to be right, but the instruction exists precisely because a wrong one
  produces a confidently wrong report. Worth watching in review; the exact-match
  data layer still prevents a bad name reaching a query.
- **It leaks markdown** (`**bold**`, `-` bullets) more than the prompt allows.
  Mitigated deterministically rather than by prompting — see
  [Output formatting](#output-formatting).
- Token usage per answer ran roughly 1.5–2.5k input, 150–950 output on the stub
  tools.
- **`reasoning_content` is a separate field** from `content`, so the model's
  reasoning does not leak into the reply. No handling needed — but note it counts
  toward `completion_tokens`, so output-token figures include thinking.

### ⚠️ Latency: the finding that matters

Measured on the free NIM tier, same question, same prompt:

| Model | Observed |
|---|---|
| `nemotron-3-ultra-550b-a55b` | **3.9s and 42.2s** |
| `nemotron-3-super-120b-a12b` | 2.0s and 6.5s |
| `nemotron-3-nano-30b-a3b` | 1.9s and 2.4s |

Ultra's spread is not a cold start — it is queueing on shared capacity. A
42-second wait in a WhatsApp chat reads as broken, and the user has no way to tell
it apart from a dead bot.

**So Ultra is not the right default for this product as it stands**, despite being
the most capable of the three. Options, in order of preference:

1. **Use `nemotron-3-super-120b-a12b`** — fast enough, still large, same tool
   calling. Set `NEMOTRON_MODEL`.
2. **Keep Ultra but on dedicated capacity** (NVIDIA's paid tier), which removes
   the queueing. Re-measure before committing to it.
3. **Stay on Claude** for the chat path. Sonnet 5 was chosen for exactly this
   reason — the user is waiting.

Whichever you pick, measure it yourself on your own account: these numbers are
one afternoon on a free key, not a benchmark.

## Model choice

**Default: `claude-sonnet-5` at `medium` effort.**

The reasoning: this is a bounded, latency-sensitive task — understand one request,
pick one tool, write two sentences. The user is waiting in a chat window, so a
reply that takes eight seconds feels broken regardless of how good it is. Sonnet
gives near-Opus tool-calling reliability at roughly half the cost and noticeably
lower latency.

`medium` rather than `low` because **picking the wrong tool or the wrong date is
the failure that matters here**, and that is worth a second. `medium` rather than
`high` because the task is not hard enough to need it.

Change either without touching code:
```
AGENT_MODEL=claude-opus-5
AGENT_EFFORT=high
```

Rough cost per question: a few hundred input tokens (system prompt, tools,
history) plus a short reply. At Sonnet 5 pricing that is well under ₹5 per query
— negligible at demo volume. Watch it as usage grows; every reply is logged with
its token counts.

---

## Output formatting

WhatsApp is not markdown: it renders `*single asterisks*` as bold and shows `##`,
`**double asterisks**` and `-` bullets as literal punctuation.

Two layers handle this, and the second is the one that matters:

1. The system prompt asks for WhatsApp-flavoured output.
2. **`agent/formatting.py` rewrites it anyway**, on every reply from every
   provider — `**bold**` → `*bold*`, headings stripped, `-` → `•`.

Prompting reduces how often there is anything to fix. The rewrite makes it not
matter. A mechanical transformation should not depend on model compliance, and
both models were observed leaking markdown under load.

---

## Today's date is in the system prompt

`system_prompt_for()` appends the current date to the prompt on every request.

This is not cosmetic. The tools take ISO dates and the prompt asks the model to
resolve "aaj"/"kal" itself. Without an anchor a model guesses from its training
data — Nemotron reached for October 2024 — the guess falls outside the stub's
90-day window, every call fails, and the whole `max_tool_turns` budget burns out
on retries. That was a real failure, found by running it.

It is computed per request so a server running past midnight does not serve
yesterday's date, and it only changes once a day, which keeps the prompt
cacheable.

---

## How the agent decides

The loop in `claude_agent.py`:

1. Send the question, the tool definitions, and recent history to the model
2. If the reply contains `tool_use` blocks → run them, send all results back in
   **one** user message, repeat
3. If it contains only text → that is the answer

Bounded at `AGENT_MAX_TOOL_TURNS` (default 6). A tool that keeps failing would
otherwise loop until the bill noticed.

**Why the loop is hand-written** rather than using the SDK's tool runner: replies
go out over WhatsApp so we need the final text rather than a stream, and every
tool call is recorded in `AgentReply.tools_used` for the audit trail a fintech
deployment will want.

### The system prompt does the heavy lifting

Three instructions in `SYSTEM_PROMPT` earn their place:

- **Answer in the user's language mix.** Without it, a Hinglish question gets a
  formal English answer, which reads as a different product.
- **Never state a figure a tool did not give you.** The failure mode this prevents
  is the expensive one — a confidently wrong number is worse than an error.
- **Confirm misheard names rather than guessing.** ASR will mangle campaign names.
  The data source enforces this too: lookup is exact-match, deliberately not
  fuzzy, so a near-miss on "Diwaali Cashbak" returns the list of real names
  instead of silently reporting on "Diwali Cashback".

---

## Plugging in the real API

One file: `agent/datasource.py`.

1. Write a class implementing `CampaignDataSource` — three read methods.
2. Add a branch in `factory.build_data_source` for it.
3. Set `AGENT_DATA_SOURCE` to the new name.

If the real API's shapes disagree with `CampaignCount` / `DeliveryReport` (likely),
change those dataclasses and the three small tools in `campaign_tools.py`. Nothing
above the data layer moves — not the agent, not the handler, not the server.

**The interface is deliberately read-only.** There is no `send_campaign` method
and there should not be: the caller is authenticated by phone number, which is not
strong enough to authorise a state change. Keeping writes absent from the interface
means no prompt can reach one. See [phase-5-auth.md](phase-5-auth.md).

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `ANTHROPIC_API_KEY is not set` | Missing from `.env` |
| Agent answers without calling a tool | Effort too low, or the question did not need data. Check the `tools=` list in the log line |
| "No campaign named X" | Working as intended — ASR misheard a name. The reply lists the real ones |
| Every reply says the same thing | `HANDLER` is still `echo` or `transcribe` |
| Answers ignore the previous message | Idle timeout elapsed (default 1h) so the thread reset |
| Slow replies | Speech transcription usually dominates, not the model. Check the `Transcribed … in Xs` log |

Each answered message logs one line:
```
Answered 919876543210 — tools=['get_campaign_count'] tokens=412/38
```

---

## Known limits

- **Stub data** — the headline caveat above.
- **In-memory conversations.** A restart forgets every thread, and a multi-worker
  deployment would split them across workers. Shared storage is a Phase 6
  prerequisite.
- **No authorisation yet.** Anyone who can reach the number gets answers — that is
  Phase 5, and it must land before any real data does.
- **Read-only by design.** The agent cannot send campaigns or change anything, and
  says so if asked.

---

## Next

Phase 5 ([auth](phase-5-auth.md)) — restrict who gets answers. Non-negotiable
before the stub is replaced with real campaign data.
