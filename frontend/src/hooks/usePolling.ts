import { useState, useEffect, useRef, useCallback } from 'react'

interface UsePollingOptions<T> {
  intervalMs?: number
  enabled?: boolean
  onData?: (data: T) => void
}

interface UsePollingResult<T> {
  data: T | null
  error: Error | null
  isLoading: boolean
  refetch: () => Promise<void>
}

export function usePolling<T>(
  fetchFn: () => Promise<T>,
  options: UsePollingOptions<T> = {},
): UsePollingResult<T> {
  const { intervalMs = 3000, enabled = true, onData } = options

  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [isLoading, setIsLoading] = useState<boolean>(false)

  // Keep stable refs so the interval callback doesn't capture stale closures
  const fetchFnRef = useRef(fetchFn)
  const onDataRef = useRef(onData)
  const isMountedRef = useRef(true)
  const inFlightRef = useRef(false)
  // Serialised form of the last payload stored in `data` — polls whose payload
  // hasn't changed skip setData so consumers don't re-render every tick.
  const lastJsonRef = useRef<string | null>(null)

  useEffect(() => {
    fetchFnRef.current = fetchFn
  }, [fetchFn])

  useEffect(() => {
    onDataRef.current = onData
  }, [onData])

  useEffect(() => {
    isMountedRef.current = true
    return () => {
      isMountedRef.current = false
    }
  }, [])

  const execute = useCallback(async () => {
    // Skip if a previous poll is still in flight, so a slow response can't stack up
    // overlapping requests behind the interval timer.
    if (inFlightRef.current) return
    inFlightRef.current = true
    // isLoading marks the initial load only (all consumers gate first-render
    // states on it); routine poll ticks don't toggle it, so a tick with an
    // unchanged payload causes no re-render at all.
    if (lastJsonRef.current === null) setIsLoading(true)
    try {
      const result = await fetchFnRef.current()
      if (!isMountedRef.current) return
      const json = JSON.stringify(result)
      if (json !== lastJsonRef.current) {
        lastJsonRef.current = json
        setData(result)
      }
      setError(null)
      onDataRef.current?.(result)
    } catch (err) {
      if (!isMountedRef.current) return
      setError(err instanceof Error ? err : new Error(String(err)))
    } finally {
      inFlightRef.current = false
      if (isMountedRef.current) {
        setIsLoading(false)
      }
    }
  }, [])

  useEffect(() => {
    if (!enabled) return

    // Fetch immediately on mount / when enabled flips to true
    void execute()

    const timerId = setInterval(() => {
      void execute()
    }, intervalMs)

    return () => {
      clearInterval(timerId)
    }
  }, [enabled, intervalMs, execute])

  return { data, error, isLoading, refetch: execute }
}
