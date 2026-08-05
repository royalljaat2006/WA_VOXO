import { useState } from 'react'
import type { Recorder } from '../useRecorder'

interface Props {
  recorder: Recorder
  busy: boolean
  /** A transfer is waiting for a PIN, so the input masks what is typed. */
  awaitingPin?: boolean
  onSendText: (text: string) => void
  onSendVoice: (audio: Blob, seconds: number) => void
}

/**
 * The mic-and-text bar.
 *
 * Press-and-hold to record, matching WhatsApp — releasing sends. Typing is kept
 * alongside it because a laptop demo in a quiet room sometimes needs it, and
 * because the backend answers text on the same path.
 */
export function Composer({
  recorder,
  busy,
  awaitingPin = false,
  onSendText,
  onSendVoice,
}: Props) {
  const [draft, setDraft] = useState('')
  const { isRecording, seconds, error, start, stop } = recorder

  async function finishRecording() {
    const captured = seconds
    const blob = await stop()
    if (blob) onSendVoice(blob, captured)
  }

  function submitText(event: React.FormEvent) {
    event.preventDefault()
    const text = draft.trim()
    if (!text || busy) return
    onSendText(text)
    setDraft('')
  }

  return (
    <div className="composer">
      {error && <div className="composer__error">{error}</div>}

      <form className="composer__row" onSubmit={submitText}>
        <input
          className="composer__input"
          // A password field during the PIN window, so the digits are not on
          // screen or in a screen recording of the demo.
          type={awaitingPin && !isRecording ? 'password' : 'text'}
          inputMode={awaitingPin ? 'numeric' : 'text'}
          autoComplete="off"
          value={isRecording ? `Recording… ${seconds}s` : draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={
            awaitingPin
              ? 'Say or type your PIN to confirm'
              : 'Type a question, or hold the mic'
          }
          disabled={isRecording || busy}
          aria-label={awaitingPin ? 'PIN' : 'Question'}
        />

        <button
          type="button"
          className={`mic ${isRecording ? 'mic--recording' : ''}`}
          // Pointer events rather than mouse/touch pairs — one set covers both,
          // and pointerleave prevents a stuck recording if the cursor slides off.
          onPointerDown={start}
          onPointerUp={finishRecording}
          onPointerLeave={() => isRecording && finishRecording()}
          disabled={busy}
          aria-label={isRecording ? 'Release to send' : 'Hold to record'}
        >
          {isRecording ? '●' : '🎤'}
        </button>

        {draft.trim() && !isRecording && (
          <button className="send" type="submit" disabled={busy} aria-label="Send">
            ➤
          </button>
        )}
      </form>

      <p className="composer__hint">
        {isRecording
          ? 'Release to send'
          : awaitingPin
            ? '🔒 Waiting for your PIN — say "cancel" to stop'
            : 'Hold the mic button and speak'}
      </p>
    </div>
  )
}
