/**
 * Microphone recording, as a hook.
 *
 * Isolated from the UI because the failure modes are browser-specific and worth
 * handling in one place: permission denied, no microphone, and a page served
 * over plain HTTP (getUserMedia requires a secure context — localhost counts,
 * a LAN IP does not).
 */

import { useCallback, useRef, useState } from 'react'

export interface Recorder {
  isRecording: boolean
  seconds: number
  error: string | null
  start: () => Promise<void>
  stop: () => Promise<Blob | null>
}

export function useRecorder(): Recorder {
  const [isRecording, setIsRecording] = useState(false)
  const [seconds, setSeconds] = useState(0)
  const [error, setError] = useState<string | null>(null)

  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const tickRef = useRef<number | null>(null)

  const cleanup = useCallback(() => {
    if (tickRef.current !== null) {
      window.clearInterval(tickRef.current)
      tickRef.current = null
    }
    // Release the microphone, or the browser keeps showing a recording indicator.
    recorderRef.current?.stream.getTracks().forEach((track) => track.stop())
  }, [])

  const start = useCallback(async () => {
    setError(null)

    if (!navigator.mediaDevices?.getUserMedia) {
      setError('This browser cannot record audio. Try Chrome over http://localhost.')
      return
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const recorder = new MediaRecorder(stream)
      chunksRef.current = []

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data)
      }
      recorder.start()
      recorderRef.current = recorder

      setIsRecording(true)
      setSeconds(0)
      tickRef.current = window.setInterval(() => setSeconds((s) => s + 1), 1000)
    } catch (cause) {
      const name = (cause as Error)?.name
      setError(
        name === 'NotAllowedError'
          ? 'Microphone permission was denied. Allow it in the address bar and try again.'
          : name === 'NotFoundError'
            ? 'No microphone found.'
            : `Could not start recording: ${(cause as Error)?.message ?? cause}`,
      )
      cleanup()
    }
  }, [cleanup])

  const stop = useCallback(async (): Promise<Blob | null> => {
    const recorder = recorderRef.current
    if (!recorder || recorder.state === 'inactive') {
      cleanup()
      setIsRecording(false)
      return null
    }

    const blob = await new Promise<Blob>((resolve) => {
      recorder.onstop = () =>
        resolve(new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' }))
      recorder.stop()
    })

    cleanup()
    setIsRecording(false)
    // A blob under a few hundred bytes is a tap, not speech — the backend would
    // just return "I didn't hear anything".
    return blob.size > 500 ? blob : null
  }, [cleanup])

  return { isRecording, seconds, error, start, stop }
}
