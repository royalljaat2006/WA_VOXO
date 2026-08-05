# Phase 3 — Voice Pipeline

**Goal:** send a voice note, get its transcript back as a text reply.

**Acceptance:** record a voice note on WhatsApp → a message comes back containing
what you said.

This is the first genuine demo moment: everything before it proved plumbing, this
proves the product idea.

---

## What was built

No new machinery — four existing pieces wired together:

```
client.fetch_media()  →  audio.to_wav()  →  transcriber.transcribe()  →  client.send_text()
     (Phase 1)              (Phase 0)            (Phase 0)                  (Phase 1)
```

`TranscribeHandler` in `whatsapp/handlers.py` is the wiring. The webhook server was
not touched — swapping behaviour is a config change, which is what the
`MessageHandler` seam exists for.

---

## Setup

Phase 3 needs Phases 1 and 2 working first (credentials + a reachable webhook).

### 1. Install the speech extras

```bash
.venv/bin/pip install -e ".[stt,server,dev]"
```

### 2. Switch the handler on

In `.env`:

```
HANDLER=transcribe
```

### 3. Pick a speech engine

Whatever Phase 0's benchmark chose. With no API keys set, local Whisper runs:

```
RUN_LOCAL_WHISPER=1
WHISPER_MODEL=large-v3
```

For a first test, `WHISPER_MODEL=tiny` starts in seconds instead of downloading
~1.5GB. Accuracy will be poor — that is expected, you are testing the pipeline.

### 4. Run

```bash
.venv/bin/python tools/run_server.py
```

then in another terminal:

```bash
ngrok http 8000
```

The model loads **at startup**, before any message arrives. The server prints
`Speech engine ready` when it can serve.

### 5. Send a voice note

Record one from your phone to the test number. Within a few seconds a reply comes
back:

> 🎤 aaj kitne campaign gaye

---

## What you should see in the logs

```
INFO  Queued AudioMessage from 919876543210
INFO  Transcribed wamid.XXX in 2.3s: aaj kitne campaign gaye
```

The latency shown is transcription only. Add roughly a second for the media
download and the reply.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Reply says "I couldn't download that voice note" | Access token expired, or the media URL timed out. Temporary tokens last ~24h — get a fresh one. |
| Reply says "I couldn't make out that recording" | The engine failed. Check the log for the underlying error — usually a bad API key or an unreachable STT service. |
| Reply says "That audio format didn't work" | `ffmpeg` is missing or the download was truncated. `brew install ffmpeg`. |
| Reply says "I didn't hear anything" | The engine returned an empty string — often silence, or a very short clip. |
| First voice note takes 30+ seconds | The model was still loading. It should load at startup; check for `Speech engine ready` in the log before testing. |
| Nothing comes back at all | The handler is still `echo`. Check `HANDLER=transcribe` in `.env` and restart. |
| Transcript is wrong / garbled | Expected with `WHISPER_MODEL=tiny`. Switch to the engine Phase 0's benchmark picked. |

---

## Known limits

- **Latency.** Local Whisper `large-v3` on a laptop CPU takes several seconds per
  clip. Acceptable for a demo, not for production — Phase 6 either moves to a
  hosted engine or a machine with a GPU.
- **No acknowledgement message.** The user waits in silence until the transcript
  arrives. If the wait feels long in the demo, sending an immediate "🎤 listening…"
  is a small change to `TranscribeHandler`.
- **Long recordings.** No length cap yet. A five-minute voice note will take a long
  time and cost accordingly.
- **Audio is deleted after processing.** Voice notes are customer data; the handler
  works in a temp directory and removes it in a `finally` block. If you need to keep
  clips for debugging, do it deliberately and remember `data/` is gitignored for
  this reason.

---

## Next

Phase 3 replies with the transcript. **Phase 4** replaces that last step: the
transcript goes to an LLM with the internal APIs defined as tools, and the reply
becomes the actual answer.

The change is one new handler. `app.py`, `client.py`, `stt/` and `audio.py` all stay
as they are.
