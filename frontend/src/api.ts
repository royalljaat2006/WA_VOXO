/**
 * The only place that talks to the backend.
 *
 * Keeping every fetch here means components never assemble URLs or headers, and
 * changing the demo token or base URL is one edit.
 */

export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'
const BASE = API_BASE
const TOKEN = import.meta.env.VITE_DEMO_TOKEN ?? ''

/** One step of the backend pipeline, with how long it took. */
export interface Stage {
  name: string
  ms: number
  detail: string
}

export interface AnswerResponse {
  transcript: string | null
  answer: string
  tools_used: string[]
  stages: Stage[]
  input_tokens: number
  output_tokens: number
  stub_data: boolean
  refused: boolean
  /**
   * A transfer is prepared and waiting for a PIN. While this is true the next
   * thing the user says is probably a secret, so the UI stops displaying it.
   */
  awaiting_pin: boolean
}

/** One line of the audit trail, as mission control shows it. */
export interface AuditEntry {
  ts: string
  event: string
  intent_id?: string | null
  payer?: string | null
  payee?: string | null
  payee_name?: string | null
  payee_source?: string | null
  amount_paise?: number | null
  reference?: string | null
  reason?: string | null
  status?: string | null
}

export interface MissionControl {
  audit_enabled: boolean
  total_events: number
  transfers_completed: number
  transfers_failed: number
  total_paise: number
  recent: AuditEntry[]
}

export interface DemoStatus {
  handler: string
  speech_engine: string
  model: string
  stub_data: boolean
  auth_enabled: boolean
  payments_enabled: boolean
  /** False while the stub gateway is in place — no money actually moves. */
  real_money: boolean
}

/** Thrown for any non-2xx, carrying the backend's own explanation. */
export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  return TOKEN ? { ...extra, 'X-Demo-Token': TOKEN } : extra
}

async function unwrap<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>

  // FastAPI puts the useful text in `detail`; show that rather than a bare code.
  let detail = `Request failed (${response.status})`
  try {
    const body = await response.json()
    if (typeof body?.detail === 'string') detail = body.detail
  } catch {
    // Non-JSON error body — keep the generic message.
  }
  throw new ApiError(detail, response.status)
}

export async function getStatus(): Promise<DemoStatus> {
  const response = await fetch(`${BASE}/demo/status`, { headers: headers() })
  return unwrap<DemoStatus>(response)
}

export async function ask(question: string, sessionId: string): Promise<AnswerResponse> {
  const response = await fetch(`${BASE}/demo/ask`, {
    method: 'POST',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ question, session_id: sessionId }),
  })
  return unwrap<AnswerResponse>(response)
}

/**
 * Simulate attaching a WhatsApp contact card.
 *
 * On WhatsApp the number arrives from the platform already verified. Here it is
 * typed — so this exercises the same path (no digit read-back) without the
 * verification that normally justifies skipping it.
 */
export async function shareContact(
  phone: string,
  name: string,
  sessionId: string,
): Promise<AnswerResponse> {
  const response = await fetch(`${BASE}/demo/contact`, {
    method: 'POST',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ phone, name: name || null, session_id: sessionId }),
  })
  return unwrap<AnswerResponse>(response)
}

export async function sendVoice(audio: Blob, sessionId: string): Promise<AnswerResponse> {
  const form = new FormData()
  // The extension matters — the backend picks the container format from it.
  form.append('audio', audio, 'recording.webm')
  form.append('session_id', sessionId)

  const response = await fetch(`${BASE}/demo/voice`, {
    method: 'POST',
    headers: headers(),
    body: form,
  })
  return unwrap<AnswerResponse>(response)
}

export async function getMissionControl(): Promise<MissionControl> {
  const response = await fetch(`${BASE}/demo/audit`, { headers: headers() })
  return unwrap<MissionControl>(response)
}
