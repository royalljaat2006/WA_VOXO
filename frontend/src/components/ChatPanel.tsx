import { useEffect, useRef } from 'react'
import type { ChatMessage } from '../types'

interface Props {
  messages: ChatMessage[]
  busy: boolean
  /** Which message's trace is open, so the selected bubble can be highlighted. */
  selectedId: string | null
  onSelect: (id: string) => void
}

const SUGGESTIONS = [
  'Ramesh ko 2000 bhej do',
  'Aaj kitne campaign gaye?',
  'Diwali Cashback ka delivery report bhej de',
]

export function ChatPanel({ messages, busy, selectedId, onSelect }: Props) {
  const endRef = useRef<HTMLDivElement>(null)

  // Keep the newest message in view, the way a chat app does.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length, busy])

  return (
    <div className="chat">
      {messages.length === 0 && (
        <div className="chat__empty">
          <p className="chat__empty-title">Hold the mic and speak</p>
          <p className="chat__empty-body">
            Any of 22 Indian languages, the way you actually would. For example:
          </p>
          <ul className="chat__suggestions">
            {SUGGESTIONS.map((s) => (
              <li key={s}>“{s}”</li>
            ))}
          </ul>
        </div>
      )}

      {messages.map((message) => (
        <button
          key={message.id}
          type="button"
          className={[
            'bubble',
            `bubble--${message.author}`,
            message.isError ? 'bubble--error' : '',
            message.isSecret ? 'bubble--secret' : '',
            selectedId === message.id ? 'bubble--selected' : '',
            message.stages?.length ? 'bubble--inspectable' : '',
          ]
            .filter(Boolean)
            .join(' ')}
          onClick={() => message.stages?.length && onSelect(message.id)}
          // Only bubbles with a trace do anything when clicked.
          disabled={!message.stages?.length}
        >
          {message.isVoice && (
            <span className="bubble__voice">
              🎤 voice note{message.seconds ? ` · ${message.seconds}s` : ''}
            </span>
          )}
          {message.isSecret && (
            <span className="bubble__voice">
              never shown, never logged, never sent to the model
            </span>
          )}
          <span className="bubble__text">{message.text}</span>
          {message.toolsUsed && message.toolsUsed.length > 0 && (
            <span className="bubble__tools">
              called {message.toolsUsed.join(', ')}
            </span>
          )}
        </button>
      ))}

      {busy && (
        <div className="bubble bubble--agent bubble--typing">
          <span className="dot" />
          <span className="dot" />
          <span className="dot" />
        </div>
      )}

      <div ref={endRef} />
    </div>
  )
}
