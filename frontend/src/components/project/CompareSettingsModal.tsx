import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  CleanupTuningParams,
  PatchProjectRequest,
  ProjectConfig,
  Segment,
  TuningPreviewStatus,
} from '../../types/api'
import {
  createTuningPreview,
  getSegments,
  getTuningPreview,
  getTuningPreviewAudioUrl,
  patchProject,
  ApiError,
} from '../../api/client'
import { usePolling } from '../../hooks/usePolling'
import {
  CLEANUP_KNOBS,
  configValues,
  type TuningKey,
  type TuningValue,
  type TuningValues,
} from '../../utils/tuning'
import { KnobFields } from './KnobFields'
import { formatDuration } from '../../utils/format'

interface CompareSettingsModalProps {
  projectId: string
  config: ProjectConfig
  /** Called after either column's settings are saved so the parent can refetch. */
  onSaved: () => void
  onClose: () => void
}

const POLL_MS = 3000
// Cleanup previews are CPU FFmpeg on one segment — near-instant once the job
// runs. Ten minutes covers a queue stuck behind long pipeline jobs.
const COMPARE_TIMEOUT_MS = 10 * 60_000

function cleanupParams(values: TuningValues): CleanupTuningParams {
  return {
    target_lufs: Number(values.target_lufs),
    highpass_hz: Number(values.highpass_hz),
    do_trim_silence: Boolean(values.do_trim_silence),
    silence_threshold_db: Number(values.silence_threshold_db),
    silence_min_duration_secs: Number(values.silence_min_duration_secs),
    silence_pad_start_secs: Number(values.silence_pad_start_secs),
    silence_pad_end_secs: Number(values.silence_pad_end_secs),
  }
}

type Phase = 'idle' | 'generating' | 'ready' | 'error'

/** Poll one tuning preview to completion and render its audio. */
function ResultPane({ projectId, previewId }: { projectId: string; previewId: string | null }) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState<string | null>(null)
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const objectUrlRef = useRef<string | null>(null)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
    }
  }, [])

  // A new submission resets the pane.
  useEffect(() => {
    if (objectUrlRef.current) {
      URL.revokeObjectURL(objectUrlRef.current)
      objectUrlRef.current = null
    }
    setObjectUrl(null)
    setError(null)
    setPhase(previewId ? 'generating' : 'idle')
  }, [previewId])

  const fetchStatus = useCallback(() => {
    return getTuningPreview(projectId, previewId ?? '')
  }, [projectId, previewId])

  const handleStatus = useCallback(
    (status: TuningPreviewStatus) => {
      if (phase !== 'generating' || !previewId) return
      if (status.status === 'complete') {
        void (async () => {
          try {
            const res = await fetch(getTuningPreviewAudioUrl(projectId, previewId))
            if (!res.ok) throw new Error(`HTTP ${res.status}`)
            const blob = await res.blob()
            if (!mountedRef.current) return
            const url = URL.createObjectURL(blob)
            objectUrlRef.current = url
            setObjectUrl(url)
            setPhase('ready')
          } catch (err) {
            if (!mountedRef.current) return
            setError(err instanceof Error ? err.message : 'Failed to load audio.')
            setPhase('error')
          }
        })()
      } else if (status.status === 'failed') {
        setError(status.error ?? 'Processing failed.')
        setPhase('error')
      }
    },
    [phase, previewId, projectId],
  )

  usePolling(fetchStatus, {
    intervalMs: POLL_MS,
    enabled: phase === 'generating' && previewId !== null,
    onData: handleStatus,
  })

  // Bounded lifetime — a hung job must not spin the poll forever.
  useEffect(() => {
    if (phase !== 'generating') return
    const timer = setTimeout(() => {
      setError('Preview timed out — check failed jobs.')
      setPhase('error')
    }, COMPARE_TIMEOUT_MS)
    return () => clearTimeout(timer)
  }, [phase])

  if (phase === 'idle') return null
  if (phase === 'generating') {
    return <p className="text-xs text-gray-500 dark:text-gray-400">Processing…</p>
  }
  if (phase === 'error') {
    return <p className="text-xs text-red-600 dark:text-red-400">{error}</p>
  }
  return (
    <audio controls src={objectUrl ?? undefined} className="w-full h-8">
      Your browser does not support audio playback.
    </audio>
  )
}

/** One side of the comparison: editable params, result audio, save button. */
function CompareColumn({
  title,
  projectId,
  values,
  onChange,
  previewId,
  idPrefix,
  onSaved,
}: {
  title: string
  projectId: string
  values: TuningValues
  onChange: (key: TuningKey, value: TuningValue) => void
  previewId: string | null
  idPrefix: string
  onSaved: () => void
}) {
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSave() {
    setSaving(true)
    setError(null)
    try {
      await patchProject(projectId, values as PatchProjectRequest)
      setSaved(true)
      onSaved()
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : err instanceof Error ? err.message : 'Save failed',
      )
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-3 rounded-lg border border-gray-200 dark:border-gray-700 p-3">
      <p className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
        {title}
      </p>
      <KnobFields
        knobs={CLEANUP_KNOBS}
        values={values}
        onChange={(key, value) => {
          setSaved(false)
          onChange(key, value)
        }}
        idPrefix={idPrefix}
      />
      <ResultPane projectId={projectId} previewId={previewId} />
      {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      {saved && !error && (
        <p className="text-xs text-green-700 dark:text-green-400">Saved as project settings.</p>
      )}
      <button
        type="button"
        onClick={() => void handleSave()}
        disabled={saving}
        className="px-3 py-1.5 text-xs font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-50"
      >
        {saving ? 'Saving…' : 'Save these settings'}
      </button>
    </div>
  )
}

// A/B test cleanup settings on one segment: two editable param columns, run
// both through the cleanup service as ephemeral previews, keep the winner.
export function CompareSettingsModal({
  projectId,
  config,
  onSaved,
  onClose,
}: CompareSettingsModalProps) {
  const [segments, setSegments] = useState<Segment[]>([])
  const [segmentId, setSegmentId] = useState<string>('')
  const [loadError, setLoadError] = useState<string | null>(null)

  const [valuesA, setValuesA] = useState<TuningValues>(() => configValues(config, CLEANUP_KNOBS))
  const [valuesB, setValuesB] = useState<TuningValues>(() => configValues(config, CLEANUP_KNOBS))
  const [previewIdA, setPreviewIdA] = useState<string | null>(null)
  const [previewIdB, setPreviewIdB] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    getSegments(projectId, { per_page: 50 })
      .then((res) => {
        if (!alive) return
        setSegments(res.segments)
        setSegmentId((prev) => prev || (res.segments[0]?.id ?? ''))
      })
      .catch((err: unknown) => {
        if (!alive) return
        setLoadError(err instanceof Error ? err.message : 'Failed to load segments.')
      })
    return () => {
      alive = false
    }
  }, [projectId])

  async function handleRun() {
    if (!segmentId) return
    setRunning(true)
    setRunError(null)
    setPreviewIdA(null)
    setPreviewIdB(null)
    try {
      const resA = await createTuningPreview(projectId, {
        stage: 'cleanup',
        params: cleanupParams(valuesA),
        target: { segment_id: segmentId },
      })
      const resB = await createTuningPreview(projectId, {
        stage: 'cleanup',
        params: cleanupParams(valuesB),
        target: { segment_id: segmentId },
      })
      setPreviewIdA(resA.enqueued_job.id)
      setPreviewIdB(resB.enqueued_job.id)
    } catch (err) {
      setRunError(
        err instanceof ApiError ? err.message : err instanceof Error ? err.message : 'Failed to start comparison.',
      )
    } finally {
      setRunning(false)
    }
  }

  function segmentLabel(seg: Segment): string {
    const text = (seg.transcript_edited ?? seg.transcript ?? '').trim()
    const excerpt = text ? `“${text.slice(0, 40)}${text.length > 40 ? '…' : ''}”` : seg.id.slice(0, 8)
    return `${excerpt} (${formatDuration(seg.duration_secs)})`
  }

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto p-5 space-y-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">
              Compare cleanup settings
            </h2>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
              Run one segment through cleanup with two different settings and keep the winner.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
          >
            ✕
          </button>
        </div>

        <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
          <span className="shrink-0">Test on</span>
          <select
            value={segmentId}
            onChange={(e) => setSegmentId(e.target.value)}
            disabled={segments.length === 0}
            className="flex-1 min-w-0 border border-gray-300 dark:border-gray-600 rounded px-2 py-1 text-sm dark:bg-gray-900 dark:text-gray-100"
          >
            {segments.length === 0 ? (
              <option value="">No segments available</option>
            ) : (
              segments.map((seg) => (
                <option key={seg.id} value={seg.id}>
                  {segmentLabel(seg)}
                </option>
              ))
            )}
          </select>
        </label>
        {loadError && <p className="text-xs text-red-600 dark:text-red-400">{loadError}</p>}

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <CompareColumn
            title="A — current settings"
            projectId={projectId}
            values={valuesA}
            onChange={(key, value) => setValuesA((prev) => ({ ...prev, [key]: value }))}
            previewId={previewIdA}
            idPrefix="cmp-a"
            onSaved={onSaved}
          />
          <CompareColumn
            title="B — draft"
            projectId={projectId}
            values={valuesB}
            onChange={(key, value) => setValuesB((prev) => ({ ...prev, [key]: value }))}
            previewId={previewIdB}
            idPrefix="cmp-b"
            onSaved={onSaved}
          />
        </div>

        {runError && <p className="text-xs text-red-600 dark:text-red-400">{runError}</p>}

        <button
          type="button"
          onClick={() => void handleRun()}
          disabled={running || !segmentId}
          className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {running ? 'Starting…' : 'Run comparison'}
        </button>
      </div>
    </div>
  )
}
