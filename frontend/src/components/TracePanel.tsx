import type { ChatMessage } from '../types'

interface Props {
  message: ChatMessage | null
}

/** Human-readable labels for the backend's stage names. */
const STAGE_LABELS: Record<string, string> = {
  upload: 'Audio received',
  convert: 'Converted to 16 kHz mono',
  transcribe: 'Speech → text',
  agent: 'Agent decided and answered',
}

/**
 * Where the seconds went.
 *
 * This is the part WhatsApp cannot show, and the reason the demo UI is worth
 * having: it makes the pipeline legible to someone who is being told about it
 * for the first time.
 */
export function TracePanel({ message }: Props) {
  if (!message?.stages?.length) {
    return (
      <aside className="trace trace--empty">
        <h2 className="trace__title">Pipeline</h2>
        <p className="trace__hint">
          Ask something, then tap a reply to see each step and how long it took.
        </p>
      </aside>
    )
  }

  const total = message.stages.reduce((sum, stage) => sum + stage.ms, 0)
  const slowest = Math.max(...message.stages.map((s) => s.ms))

  return (
    <aside className="trace">
      <h2 className="trace__title">Pipeline</h2>
      <p className="trace__total">
        <strong>{(total / 1000).toFixed(1)}s</strong> end to end
      </p>

      <ol className="trace__stages">
        {message.stages.map((stage, index) => (
          <li key={`${stage.name}-${index}`} className="stage">
            <div className="stage__head">
              <span className="stage__name">
                {STAGE_LABELS[stage.name] ?? stage.name}
              </span>
              <span className="stage__ms">{stage.ms} ms</span>
            </div>
            {stage.detail && <div className="stage__detail">{stage.detail}</div>}
            {/* Bar is relative to the slowest stage, so the bottleneck is obvious. */}
            <div className="stage__bar">
              <div
                className="stage__bar-fill"
                style={{ width: `${slowest ? (stage.ms / slowest) * 100 : 0}%` }}
              />
            </div>
          </li>
        ))}
      </ol>

      {message.toolsUsed && message.toolsUsed.length > 0 && (
        <section className="trace__section">
          <h3 className="trace__subtitle">Tools called</h3>
          <ul className="trace__tools">
            {message.toolsUsed.map((tool, index) => (
              <li key={`${tool}-${index}`}>
                <code>{tool}</code>
              </li>
            ))}
          </ul>
        </section>
      )}

      {(message.inputTokens ?? 0) > 0 && (
        <section className="trace__section">
          <h3 className="trace__subtitle">Tokens</h3>
          <p className="trace__tokens">
            {message.inputTokens} in · {message.outputTokens} out
          </p>
        </section>
      )}
    </aside>
  )
}
