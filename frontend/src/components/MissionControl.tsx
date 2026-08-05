import type { AuditEntry, MissionControl as Mission } from '../api'

interface Props {
  mission: Mission | null
}

/** Paise → "₹2,000.00" with Indian digit grouping. */
function rupees(paise: number): string {
  const whole = Math.floor(Math.abs(paise) / 100)
  const rest = String(Math.abs(paise) % 100).padStart(2, '0')
  let digits = String(whole)
  if (digits.length > 3) {
    const head = digits.slice(0, -3).replace(/(\d)(?=(\d\d)+$)/g, '$1,')
    digits = `${head},${digits.slice(-3)}`
  }
  return `₹${digits}.${rest}`
}

/** Colour and label per event. The vocabulary is closed, so this can be a map. */
const EVENTS: Record<string, { label: string; tone: string }> = {
  intent_prepared: { label: 'prepared', tone: 'neutral' },
  transfer_executed: { label: 'executed', tone: 'good' },
  transfer_failed: { label: 'failed', tone: 'bad' },
  transfer_refused: { label: 'refused', tone: 'bad' },
  pin_attempt_failed: { label: 'wrong PIN', tone: 'warn' },
  intent_cancelled: { label: 'cancelled', tone: 'warn' },
  intent_expired: { label: 'expired', tone: 'warn' },
}

function time(ts: string): string {
  return ts.slice(11, 19)
}

function describe(entry: AuditEntry): string {
  const who = entry.payee_name || entry.payee || '—'
  if (entry.reference) return `${who} · ${entry.reference}`
  if (entry.reason) return `${who} · ${entry.reason.replace(/_/g, ' ')}`
  return who
}

/**
 * Point 6 of the process: status, and review/audit.
 *
 * Fed from the audit file rather than from a parallel in-memory tally — a
 * dashboard sourced from anywhere other than the evidence is a dashboard that
 * can disagree with it.
 */
export function MissionControl({ mission }: Props) {
  if (!mission) return null

  if (!mission.audit_enabled) {
    return (
      <section className="mission">
        <h2 className="mission__title">Mission control</h2>
        <p className="mission__empty">
          No audit trail configured. Set PAYMENT_AUDIT_PATH to record transfers.
        </p>
      </section>
    )
  }

  return (
    <section className="mission" aria-label="Mission control">
      <h2 className="mission__title">Mission control</h2>

      <div className="mission__stats">
        <div className="stat">
          <span className="stat__value">{mission.transfers_completed}</span>
          <span className="stat__label">completed</span>
        </div>
        <div className="stat">
          <span className="stat__value">{rupees(mission.total_paise)}</span>
          <span className="stat__label">moved</span>
        </div>
        <div className="stat">
          <span className="stat__value">{mission.transfers_failed}</span>
          <span className="stat__label">failed / refused</span>
        </div>
        <div className="stat">
          <span className="stat__value">{mission.total_events}</span>
          <span className="stat__label">audit records</span>
        </div>
      </div>

      {mission.recent.length === 0 ? (
        <p className="mission__empty">Nothing recorded yet.</p>
      ) : (
        <div className="mission__scroll">
          <table className="mission__table">
            <thead>
              <tr>
                <th>time</th>
                <th>event</th>
                <th>amount</th>
                <th>payee</th>
                <th>source</th>
              </tr>
            </thead>
            <tbody>
              {mission.recent.map((entry, index) => {
                const meta = EVENTS[entry.event] ?? { label: entry.event, tone: 'neutral' }
                return (
                  <tr key={`${entry.ts}-${index}`}>
                    <td className="mono">{time(entry.ts)}</td>
                    <td>
                      <span className={`tag tag--${meta.tone}`}>{meta.label}</span>
                    </td>
                    <td className="mono">
                      {entry.amount_paise != null ? rupees(entry.amount_paise) : '—'}
                    </td>
                    <td>{describe(entry)}</td>
                    <td className="mono">{entry.payee_source ?? '—'}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="mission__note">
        Append-only. Written before and after every transfer. Numbers are masked
        here — the file itself keeps them in full, and is personal data.
      </p>
    </section>
  )
}
