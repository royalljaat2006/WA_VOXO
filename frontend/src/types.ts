import type { Stage } from './api'

/** A bubble in the chat transcript. */
export interface ChatMessage {
  id: string
  author: 'user' | 'agent'
  /** What the bubble shows. For a voice note this is the transcript. */
  text: string
  /** Present when the user spoke rather than typed — drives the 🎤 treatment. */
  isVoice?: boolean
  /** Recording length in seconds, shown on the user's voice bubble. */
  seconds?: number
  /** Set on error bubbles so they render distinctly. */
  isError?: boolean
  /**
   * The user said something during the PIN window. `text` holds a mask, never
   * what was actually said — see App.tsx.
   */
  isSecret?: boolean
  /** Backend trace for this exchange, shown in the side panel. */
  stages?: Stage[]
  toolsUsed?: string[]
  inputTokens?: number
  outputTokens?: number
}
