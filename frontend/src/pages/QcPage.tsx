import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import type {
  CleanupTuningParams,
  ProjectDetail,
  Segment,
  TuningPreviewStatus,
} from '../types/api'
import {
  createTuningPreview,
  getProject,
  getSegmentAudioUrl,
  getSegments,
  getTuningPreview,
  getTuningPreviewAudioUrl,
} from '../api/client'
import { usePolling } from '../hooks/usePolling'
import { EXPORTABLE_STATUSES_CSV } from '../constants'
import { errorMessage } from '../utils/errors'
import { formatDuration } from '../utils/format'

const POLL_MS = 3000
// Cleanup is CPU FFmpeg on one segment — near-instant once the job runs. Ten
// minutes covers a queue stuck behind long pipeline jobs.
const PREVIEW_TIMEOUT_MS = 10 * 60_000

function cleanupParams(config: ProjectDetail['config']): CleanupTuningParams {
  return {
    target_lufs: config.target_lufs,
    highpass_hz: config.highpass_hz,
    silence_threshold_db: config.silence_threshold_db,
    silence_min_duration_secs: config.silence_min_duration_secs,
  }
}

function segmentLabel(seg: Segment): string {
  const text = (seg.transcript_edited ?? seg.transcript ?? '').trim()
  return text ? `${text.slice(0, 60)}${text.length > 60 ? '…' : ''}` : seg.id.slice(0, 8)
}

// The "after clean" player: render the segment through the cleanup service with
// the project's saved settings (an ephemeral tuning preview) and play it. Same
// create→poll→blob lifecycle as CompareSettingsModal's ResultPane. Kept mounted
// while a segment is selected so toggling Raw⇄Clean is instant.
function CleanAudio({
  projectId,
  segmentId,
  params,
}: {
  projectId: string
  segmentId: string
  params: CleanupTuningParams
}) {
  const [previewId, setPreviewId] = useState<string | null>(null)
  const [phase, setPhase] = useState<'starting' | 'generating' | 'ready' | 'error'>('starting')
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

  // Kick off one preview per segment. params is config-stable for the page's
  // lifetime, so it is intentionally not a dependency.
  useEffect(() => {
    let alive = true
    setPhase('starting')
    setError(null)
    createTuningPreview(projectId, { stage: 'cleanup', params, target: { segment_id: segmentId } })
      .then((res) => {
        if (!alive) return
        setPreviewId(res.enqueued_job.id)
        setPhase('generating')
      })
      .catch((err: unknown) => {
        if (!alive) return
        setError(errorMessage(err, 'Failed to start cleanup preview'))
        setPhase('error')
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, segmentId])

  const fetchStatus = useCallback(
    () => getTuningPreview(projectId, previewId ?? ''),
    [projectId, previewId],
  )

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
        setError(status.error ?? 'Cleanup preview failed.')
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

  useEffect(() => {
    if (phase !== 'generating' && phase !== 'starting') return
    const timer = setTimeout(() => {
      setError('Preview timed out — check failed jobs.')
      setPhase('error')
    }, PREVIEW_TIMEOUT_MS)
    return () => clearTimeout(timer)
  }, [phase])

  if (phase === 'error') return <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
  if (phase !== 'ready') return <p className="text-sm text-gray-500 dark:text-gray-400">Cleaning…</p>
  return <audio controls src={objectUrl ?? undefined} className="w-full h-9" />
}

function SegmentDetail({ project, segment }: { project: ProjectDetail; segment: Segment }) {
  const [tab, setTab] = useState<'raw' | 'clean'>('raw')
  // Once Clean is opened for this segment, keep CleanAudio mounted (hidden on
  // the Raw tab) so toggling back and forth doesn't regenerate the preview.
  const [cleanStarted, setCleanStarted] = useState(false)

  // Reset when the selected segment changes.
  useEffect(() => {
    setTab('raw')
    setCleanStarted(false)
  }, [segment.id])

  const params = cleanupParams(project.config)
  const tabClass = (active: boolean) =>
    `px-3 py-1.5 text-sm font-medium rounded-lg transition-colors ${
      active
        ? 'bg-blue-600 text-white'
        : 'text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700/50'
    }`

  return (
    <div className="space-y-4">
      <div>
        <p className="text-sm text-gray-900 dark:text-gray-100">{segmentLabel(segment)}</p>
        <p className="mt-0.5 text-xs text-gray-400 dark:text-gray-500">
          {segment.source_filename} · {formatDuration(segment.duration_secs)} · {segment.status}
        </p>
      </div>

      <div className="flex items-center gap-2">
        <button type="button" onClick={() => setTab('raw')} className={tabClass(tab === 'raw')}>
          Before (raw)
        </button>
        <button
          type="button"
          onClick={() => {
            setTab('clean')
            setCleanStarted(true)
          }}
          className={tabClass(tab === 'clean')}
        >
          After clean
        </button>
      </div>

      <div className={tab === 'raw' ? '' : 'hidden'}>
        <audio controls preload="none" src={getSegmentAudioUrl(project.id, segment.id)} className="w-full h-9" />
      </div>
      <div className={tab === 'clean' ? '' : 'hidden'}>
        {cleanStarted && <CleanAudio projectId={project.id} segmentId={segment.id} params={params} />}
      </div>

      <p className="text-xs text-gray-400 dark:text-gray-500">
        “After clean” renders this segment through the cleanup service with the project's current
        settings — the same processing Export and dataset builds apply.
      </p>
    </div>
  )
}

// Dedicated QC view: play every segment in the export set (approved +
// auto_approved + clipping_warning) before and after cleanup, so you can verify
// what the dataset/export will actually contain.
export function QcPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const [project, setProject] = useState<ProjectDetail | null>(null)
  const [segments, setSegments] = useState<Segment[]>([])
  const [selectedId, setSelectedId] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!projectId) return
    let alive = true
    setLoading(true)
    Promise.all([
      getProject(projectId),
      getSegments(projectId, { status: EXPORTABLE_STATUSES_CSV, per_page: 200, sort: 'start_secs', order: 'asc' }),
    ])
      .then(([proj, segs]) => {
        if (!alive) return
        setProject(proj)
        setSegments(segs.segments)
        setSelectedId((prev) => prev || (segs.segments[0]?.id ?? ''))
      })
      .catch((err: unknown) => {
        if (!alive) return
        setError(errorMessage(err, 'Failed to load QC data'))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [projectId])

  const selected = segments.find((s) => s.id === selectedId) ?? null

  return (
    <div className="max-w-5xl mx-auto px-6 py-8">
      <div className="flex items-start justify-between gap-4 mb-6">
        <div className="min-w-0">
          <Link
            to={`/projects/${projectId}`}
            className="inline-block mb-1 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 text-sm"
          >
            ← {project?.name ?? 'Project'}
          </Link>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Cleaned-audio QC</h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Before/after cleanup for every segment that will ship.
          </p>
        </div>
      </div>

      {loading && <p className="text-sm text-gray-500 dark:text-gray-400">Loading…</p>}
      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      {!loading && !error && segments.length === 0 && (
        <p className="text-sm text-gray-500 dark:text-gray-400">
          No approved segments yet — approve segments in review, then come back to QC them.
        </p>
      )}

      {!loading && !error && project && segments.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-[minmax(0,20rem)_1fr] gap-6">
          <ul className="space-y-1 max-h-[70vh] overflow-y-auto pr-1">
            {segments.map((seg) => (
              <li key={seg.id}>
                <button
                  type="button"
                  onClick={() => setSelectedId(seg.id)}
                  className={`w-full text-left px-3 py-2 rounded-lg text-sm transition-colors ${
                    seg.id === selectedId
                      ? 'bg-blue-50 dark:bg-blue-900/30 text-blue-900 dark:text-blue-100'
                      : 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800'
                  }`}
                >
                  <span className="block truncate">{segmentLabel(seg)}</span>
                  <span className="block text-xs text-gray-400 dark:text-gray-500">
                    {formatDuration(seg.duration_secs)} · {seg.status}
                  </span>
                </button>
              </li>
            ))}
          </ul>

          <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-4">
            {selected ? (
              <SegmentDetail project={project} segment={selected} />
            ) : (
              <p className="text-sm text-gray-500 dark:text-gray-400">Select a segment.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
