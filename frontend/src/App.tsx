import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  API_BASE,
  ApiError,
  ask,
  getMissionControl,
  getStatus,
  sendVoice,
  shareContact,
  type AnswerResponse,
  type DemoStatus,
  type MissionControl as Mission,
} from './api'
import { ChatPanel } from './components/ChatPanel'
import { Composer } from './components/Composer'
import { MissionControl } from './components/MissionControl'
import { StatusBar } from './components/StatusBar'
import { TracePanel } from './components/TracePanel'
import type { ChatMessage } from './types'
import { useRecorder } from './useRecorder'

/** Scopes conversation history so follow-ups work, and resets on reload. */
const SESSION_ID = `demo-${Math.random().toString(36).slice(2, 10)}`

let counter = 0
const nextId = () => `m${++counter}`

export default function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<DemoStatus | null>(null)
  const [statusError, setStatusError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  // True between "₹2,000 Ramesh ko — PIN boliye" and the receipt. While it is
  // set, whatever the user says next is masked rather than displayed: the
  // backend already refuses to return the transcript, and this stops the typed
  // path from putting a PIN on screen.
  const [awaitingPin, setAwaitingPin] = useState(false)
  const [mission, setMission] = useState<Mission | null>(null)

  const recorder = useRecorder()

  useEffect(() => {
    getStatus()
      .then(setStatus)
      .catch((cause: unknown) =>
        setStatusError(
          cause instanceof ApiError
            ? cause.message
            : `Could not reach ${API_BASE} from ${window.location.origin}. ` +
              'Either the server is not running (with DEMO_ENABLED=true), or ' +
              'this page is on a port the backend does not allow — a browser ' +
              'blocks that before the request is sent, so it looks identical ' +
              'to a server that is down. Check DEMO_ALLOWED_ORIGINS.',
        ),
      )
  }, [])

  // Refreshed after every exchange rather than on a timer: the only thing that
  // writes to the trail is a turn, so polling would mostly ask nothing.
  const refreshMission = useCallback(() => {
    getMissionControl()
      .then(setMission)
      .catch(() => {
        /* Mission control is a view. It must never break the conversation. */
      })
  }, [])

  useEffect(refreshMission, [refreshMission])

  const append = useCallback((message: ChatMessage) => {
    setMessages((current) => [...current, message])
    return message.id
  }, [])

  /** Shared tail of both send paths: show the answer, or an explained failure. */
  const exchange = useCallback(
    async (request: Promise<AnswerResponse>, userMessageId: string) => {
      setBusy(true)
      try {
        const result = await request

        // A voice bubble starts as "…" and is filled in once we know what was heard.
        if (result.transcript) {
          setMessages((current) =>
            current.map((m) =>
              m.id === userMessageId ? { ...m, text: result.transcript! } : m,
            ),
          )
        }

        const replyId = nextId()
        append({
          id: replyId,
          author: 'agent',
          text: result.answer,
          stages: result.stages,
          toolsUsed: result.tools_used,
          inputTokens: result.input_tokens,
          outputTokens: result.output_tokens,
        })
        setSelectedId(replyId)
        setAwaitingPin(result.awaiting_pin)
      } catch (cause) {
        append({
          id: nextId(),
          author: 'agent',
          isError: true,
          text:
            cause instanceof ApiError
              ? cause.message
              : 'Could not reach the backend. Is it running?',
        })
      } finally {
        setBusy(false)
        refreshMission()
      }
    },
    [append, refreshMission],
  )

  const handleText = useCallback(
    (text: string) => {
      // The bubble gets the mask, not the text. The PIN still has to go to the
      // backend — that is where it is verified — but it never enters React state.
      const id = append({
        id: nextId(),
        author: 'user',
        text: awaitingPin ? '🔒 ••••' : text,
        isSecret: awaitingPin,
      })
      void exchange(ask(text, SESSION_ID), id)
    },
    [append, exchange, awaitingPin],
  )

  const handleVoice = useCallback(
    (audio: Blob, seconds: number) => {
      const id = append({
        id: nextId(),
        author: 'user',
        // Normally replaced by the transcript when it comes back. During the PIN
        // window the backend returns no transcript, so this placeholder stands.
        text: awaitingPin ? '🔒 PIN spoken' : '…',
        isVoice: true,
        isSecret: awaitingPin,
        seconds,
      })
      void exchange(sendVoice(audio, SESSION_ID), id)
    },
    [append, exchange, awaitingPin],
  )

  const handleShareContact = useCallback(
    (phone: string, name: string) => {
      const id = append({
        id: nextId(),
        author: 'user',
        text: `📇 ${name || phone} · ${phone}`,
      })
      void exchange(shareContact(phone, name, SESSION_ID), id)
    },
    [append, exchange],
  )

  const selected = useMemo(
    () => messages.find((m) => m.id === selectedId) ?? null,
    [messages, selectedId],
  )

  return (
    <div className="app">
      <header className="app__header">
        <h1 className="app__title">eko Voice Agent</h1>
        <p className="app__subtitle">
          Say it, confirm it with a PIN, keep the receipt — the same pipeline
          WhatsApp uses, minus Meta
        </p>
      </header>

      <StatusBar status={status} error={statusError} />

      <main className="app__body">
        <section className="phone" aria-label="Conversation">
          <div className="phone__bar">
            <span className="phone__avatar">eko</span>
            <div>
              <div className="phone__name">eko</div>
              <div className="phone__presence">{busy ? 'typing…' : 'online'}</div>
            </div>
          </div>

          <ChatPanel
            messages={messages}
            busy={busy}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />

          <ContactShare busy={busy} onShare={handleShareContact} />

          <Composer
            recorder={recorder}
            busy={busy}
            awaitingPin={awaitingPin}
            onSendText={handleText}
            onSendVoice={handleVoice}
          />
        </section>

        <TracePanel message={selected} />
      </main>

      <MissionControl mission={mission} />
    </div>
  )
}

/**
 * Stands in for attaching a WhatsApp contact card.
 *
 * On WhatsApp the number arrives from the platform already verified, which is
 * what justifies skipping the digit read-back. Typed here, it is a simulation of
 * that path — worth saying out loud when demonstrating it.
 */
function ContactShare({
  busy,
  onShare,
}: {
  busy: boolean
  onShare: (phone: string, name: string) => void
}) {
  const [phone, setPhone] = useState('')
  const [name, setName] = useState('')

  function submit(event: React.FormEvent) {
    event.preventDefault()
    const digits = phone.replace(/\D/g, '')
    if (digits.length < 10 || busy) return
    onShare(digits, name.trim())
    setPhone('')
    setName('')
  }

  return (
    <form className="contact-share" onSubmit={submit}>
      <input
        className="contact-share__input"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Contact name"
        aria-label="Contact name"
      />
      <input
        className="contact-share__input"
        value={phone}
        onChange={(e) => setPhone(e.target.value)}
        placeholder="Phone number"
        inputMode="numeric"
        aria-label="Contact phone number"
      />
      <button className="contact-share__button" type="submit" disabled={busy}>
        📇 Share contact
      </button>
    </form>
  )
}
