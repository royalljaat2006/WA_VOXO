import type { DemoStatus } from '../api'

interface Props {
  status: DemoStatus | null
  error: string | null
}

/**
 * What is actually running behind this demo.
 *
 * The stub-data warning is the important part: those numbers are invented, and
 * a demo audience has no other way to know that.
 */
export function StatusBar({ status, error }: Props) {
  if (error) {
    return (
      <div className="status status--error">
        <strong>Backend unreachable.</strong> {error}
      </div>
    )
  }

  if (!status) {
    return <div className="status status--muted">Connecting to backend…</div>
  }

  return (
    <div className="status">
      <div className="status__chips">
        <span className="chip">
          <span className="chip__key">handler</span> {status.handler}
        </span>
        <span className="chip">
          <span className="chip__key">speech</span> {status.speech_engine}
        </span>
        <span className="chip">
          <span className="chip__key">model</span> {status.model}
        </span>
        {status.payments_enabled && (
          <span className="chip">
            <span className="chip__key">payments</span>{' '}
            {status.real_money ? 'LIVE' : 'test'}
          </span>
        )}
      </div>
      {status.stub_data && (
        <div className="status__warning">
          ⚠ Demo data — every figure below is fake. eko's API is not connected yet.
        </div>
      )}
      {status.payments_enabled && !status.real_money && (
        <div className="status__warning">
          ⚠ Test payments — transfers are simulated and no money moves. The PIN is a
          fixed test value.
        </div>
      )}
      {status.real_money && (
        <div className="status__warning status__warning--danger">
          🔴 LIVE PAYMENTS — transfers on this instance move real money.
        </div>
      )}
    </div>
  )
}
