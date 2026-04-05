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
    setIsLoading(true)
    try {
      const result = await fetchFnRef.current()
      if (!isMountedRef.current) return
      setData(result)
      setError(null)
      onDataRef.current?.(result)
    } catch (err) {
      if (!isMountedRef.current) return
      setError(err instanceof Error ? err : new Error(String(err)))
    } finally {
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
