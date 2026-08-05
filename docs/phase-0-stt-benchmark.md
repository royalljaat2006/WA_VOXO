# Phase 0 — STT Benchmark

**Question this answers:** does any speech-to-text engine hear Hinglish plus eko's
campaign jargon well enough to build an agent on top of?

If the answer is no, nothing in Phases 1–6 can rescue it. That is why this runs first.

---

## Setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[stt,dev]"
cp .env.example .env
```

`.env` needs nothing to start — local Whisper runs without any key. Add
`SARVAM_API_KEY`, `OPENAI_API_KEY` or `ELEVENLABS_API_KEY` to include those engines.

---

## Recording the audio

Put voice notes in `data/audio/`. `.opus .ogg .m4a .mp3 .wav .aac .flac .amr` all work.

**Two ways to get them:**

1. **Record directly** — Voice Memos on Mac, or a voice note to yourself on WhatsApp.
   Fine for Phase 0: we are testing whether the *engine* understands Hinglish, not
   whether it survives WhatsApp's codec.
2. **Export from WhatsApp** — in a chat, `⋮ → More → Export chat → Attach media`.
   Voice notes come out as `.opus`.

**What to say — this matters more than how many.**

Say what a real user would say, in the register they would say it. The benchmark is
only as honest as the audio.

- "aaj kitne campaign gaye?"
- "kal ke campaign ka delivery report bhej de"
- "October wale festive campaign mein kitne deliver hue the?"
- "pichhle hafte ka summary chahiye"

Include the things ASR reliably gets wrong:
- **campaign and product names** — the actual internal ones, not generic words
- **numbers, dates, amounts**
- **code-switching mid-sentence** — the real Hinglish pattern, not clean Hindi or clean English

Aim for 10–15 clips. Also record a couple in realistic conditions — background noise,
speaking quickly — because that is what production will get.

---

## Running

```bash
.venv/bin/python tools/benchmark_stt.py
```

First run downloads the Whisper model (~1.5GB for `large-v3`, one time).

Options:
```bash
.venv/bin/python tools/benchmark_stt.py --audio-dir data/audio --output-dir data/results
```

Output lands in `data/results/`:
- `comparison.md` — transcripts side by side, per file
- `raw.json` — the same data, for further analysis

---

## Optional: accuracy numbers

Create `data/ground_truth.json` with what you actually said:

```json
{
  "note_01.opus": "aaj kitne campaign gaye",
  "note_02.opus": "kal ke campaign ka delivery report bhej de"
}
```

The benchmark then reports WER (word error rate) per engine.

> ⚠️ **Do not rank engines by WER alone.** One engine writes "aaj", another writes
> "आज" — identical meaning, WER 100%. There is a test documenting exactly this trap
> (`tests/test_metrics.py::test_script_difference_scores_as_total_miss`).
> WER is a tiebreaker, not a verdict.

---

## Reading the results

Judge in this order:

1. **Domain words** — did it get the campaign names, product names, jargon?
   This is where engines actually differ and where errors are most expensive.
2. **Numbers and dates** — "12" heard as "20" produces a confidently wrong answer,
   which is worse than a failure.
3. **Latency** — over ~10s feels broken in a chat. Compare against the audio duration.
4. **Failures** — anything erroring or returning empty.
5. **Script consistency** — an engine that mixes Devanagari and Latin unpredictably
   makes downstream matching harder.

Then record the chosen engine in the decision log in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §8, and set it in `.env`.

---

## Results

Benchmarked on `data/audio/` (9 clips: 3 English, 3 Hindi, 3 Hinglish — campaign/
delivery-report phrasing) against `data/ground_truth.json`, plus two longer clips
(15s, 124s) without ground truth to check duration handling.

| Engine | Avg latency | Domain accuracy | Notes |
|---|---|---|---|
| `local-whisper/large-v3-turbo` | ~11s | Real content errors on Hindi — e.g. heard "फेल" (fail) as "खेल" (game) in `hi_3`, changing the meaning. Frequent spelling drift (कैम्पेण, दिलिवरी) | Free, no key, no per-minute cost. Latency alone rules it out for a chat product |
| `sarvam/saarika:v2.5` | **~0.7s** | Consistently correct spelling on Hindi/Hinglish; no content errors observed. Forces script to Devanagari — a pure-English clip (`en_2`) came back transliterated rather than in Latin script | ~15x faster than local Whisper. **Rejects audio over 30s** (`HTTP 400`, points at their batch API) — hit on the 124s test clip |

Judged by reading transcripts, not the WER column — see the script-mismatch
warning above; it fired here too (Devanagari vs Latin scoring as 100% wrong for
text that was correct).

**Chosen engine: Sarvam** (`STT_ENGINE=sarvam` in `.env`), for latency alone —
11s vs 0.7s decides a chat product on its own — with better Hindi/Hinglish
accuracy as a second reason. The English-transliteration behaviour is not a
concern here: production traffic is Hinglish/Hindi voice notes, not pure English.

**Known gap carried into Phase 3:** the 30-second cap. WhatsApp voice notes can
run longer than that. Not yet handled — chunking the audio or moving to Sarvam's
batch API is follow-up work, not blocking, since most voice notes are short. If a
user sends a long one, `TranscribeHandler`'s existing `TranscriptionError` path
will surface it as "I couldn't make out that recording" rather than crash — but
the wording doesn't yet say why. Worth revisiting before a real launch.
