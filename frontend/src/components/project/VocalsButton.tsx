import { useEffect, useRef, useState } from 'react'
import { getSourceVocalsUrl } from '../../api/client'

interface VocalsButtonProps {
  projectId: string
  sourceId: string
  filename: string
}

// Fetch-on-click player for a source's separated vocals stem. Stems can be
// large (whole-episode WAVs), so nothing downloads until the user asks.
export function VocalsButton({ projectId, sourceId, filename }: VocalsButtonProps) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const objectUrlRef = useRef<string | null>(null)

  useEffect(() => {
    return () => {
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
    }
  }, [])

  async function handleLoad() {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(getSourceVocalsUrl(projectId, sourceId))
      // A non-2xx body is the JSON error envelope — never feed it to <audio>.
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
      const url = URL.createObjectURL(blob)
      objectUrlRef.current = url
      setObjectUrl(url)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load vocals.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center gap-2 min-w-0">
      <span className="truncate text-xs text-gray-500 dark:text-gray-400">{filename}</span>
      {objectUrl ? (
        <audio controls src={objectUrl} className="h-8 flex-1 min-w-0">
          Your browser does not support audio playback.
        </audio>
      ) : (
        <button
          type="button"
          onClick={() => void handleLoad()}
          disabled={loading}
          className="shrink-0 px-2 py-0.5 text-xs font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-50"
        >
          {loading ? 'Loading…' : '▶ vocals'}
        </button>
      )}
      {error && <span className="text-xs text-red-600 dark:text-red-400">{error}</span>}
    </div>
  )
}
