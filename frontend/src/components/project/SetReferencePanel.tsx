import { useEffect, useRef, useState, ChangeEvent } from 'react'
import type { ProjectDetail, ScoutStatus, SpeakerCandidate } from '../../types/api'
import {
  ApiError,
  startScout,
  getScoutStatus,
  getScoutSampleUrl,
  selectScoutSpeaker,
  continuePipeline,
  uploadReference,
} from '../../api/client'
import { formatDuration } from '../../utils/format'
import { usePolling } from '../../hooks/usePolling'

// Minimum montage length (seconds) the orchestrator accepts as a reference.
// The card's "Use this voice" is disabled below this so the user never hits
// the 422 reference_too_short from the select endpoint.
const MIN_REFERENCE_SECS = 5

// Sources whose vocals stem is ready — the only ones a scout can run against.
const SCOUTABLE_STATUSES = new Set(['diarisation_pending', 'diarisation_running', 'diarisation_failed', 'complete'])

interface SetReferencePanelProps {
  project: ProjectDetail
  onAction: () => void
  pollIntervalMs?: number
}

type Tab = 'find' | 'upload'

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback
}

export function SetReferencePanel({ project, onAction, pollIntervalMs = 3000 }: SetReferencePanelProps) {
  const [tab, setTab] = useState<Tab>('find')

  const scoutableSources = project.stats.source_coverage.filter((s) =>
    SCOUTABLE_STATUSES.has(s.status),
  )

  const [selectedSourceId, setSelectedSourceId] = useState<string>(
    () => scoutableSources[0]?.source_id ?? '',
  )
  const [scout, setScout] = useState<ScoutStatus | null>(null)
  const [scanning, setScanning] = useState(false)
  const [selectingLabel, setSelectingLabel] = useState<string | null>(null)
  const [findError, setFindError] = useState<string | null>(null)

  const [uploadProgress, setUploadProgress] = useState<number | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const uploadInputRef = useRef<HTMLInputElement>(null)

  const [continuing, setContinuing] = useState(false)
  const [continueError, setContinueError] = useState<string | null>(null)

  // Fetch existing scout results once on mount so a prior scan's candidates
  // (or an in-flight scan) show without re-scanning. A never-run scout 404s
  // with no_scout — that's expected, not an error.
  useEffect(() => {
    let cancelled = false
    getScoutStatus(project.id)
      .then((result) => {
        if (cancelled) return
        setScout(result)
      })
      .catch((err) => {
        if (cancelled) return
        if (err instanceof ApiError && err.error === 'no_scout') return
        setFindError(errorMessage(err, 'Failed to load scout status'))
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id])

  // Poll scout status while a scan is running — derived purely from fetched
  // state, so one failed poll can't wedge the panel: usePolling keeps its
  // interval alive through errors (we surface a transient "retrying" note) and
  // the loop stops itself when the fetched status leaves 'running'.
  const scoutRunning = scout?.status === 'running'
  const { error: pollError } = usePolling<ScoutStatus>(
    () => getScoutStatus(project.id),
    {
      intervalMs: pollIntervalMs,
      enabled: scoutRunning,
      onData: setScout,
    },
  )

  async function handleScan() {
    if (!selectedSourceId) return
    setFindError(null)
    setScanning(true)
    try {
      await startScout(project.id, selectedSourceId)
      // Setting a running status starts the poll loop (it's derived from scout state).
      setScout({ status: 'running', progress: 0, source_id: selectedSourceId, speakers: [] })
    } catch (err) {
      setFindError(errorMessage(err, 'Failed to start scan'))
    } finally {
      setScanning(false)
    }
  }

  async function handleSelect(label: string) {
    setFindError(null)
    setSelectingLabel(label)
    try {
      await selectScoutSpeaker(project.id, label)
      onAction()
    } catch (err) {
      setFindError(errorMessage(err, 'Failed to select speaker'))
    } finally {
      setSelectingLabel(null)
    }
  }

  async function handleUpload(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setUploadError(null)
    setUploadProgress(0)
    try {
      await uploadReference(project.id, file, (f) => setUploadProgress(f))
      onAction()
    } catch (err) {
      setUploadError(errorMessage(err, 'Upload failed'))
    } finally {
      setUploadProgress(null)
    }
  }

  async function handleContinue() {
    setContinueError(null)
    setContinuing(true)
    try {
      await continuePipeline(project.id)
      onAction()
    } catch (err) {
      setContinueError(errorMessage(err, 'Failed to continue pipeline'))
    } finally {
      setContinuing(false)
    }
  }

  const candidates: SpeakerCandidate[] =
    scout && scout.speakers ? [...scout.speakers].sort((a, b) => b.total_secs - a.total_secs) : []

  const isScanning = scanning || scoutRunning

  function referenceLabel(): string | null {
    const origin = project.reference_origin
    if (!origin) return null
    if (origin.type === 'uploaded') return 'Reference: uploaded clip'
    const source = project.stats.source_coverage.find((s) => s.source_id === origin.source_id)
    const name = source?.filename ?? origin.source_id
    return `Reference: ${origin.speaker_label} from ${name}`
  }

  const currentReference = referenceLabel()
  const canContinue = project.reference_path != null

  return (
    <div className="space-y-4">
      {/* Tabs */}
      <div className="flex gap-1 border-b border-gray-200 dark:border-gray-700">
        <TabButton active={tab === 'find'} onClick={() => setTab('find')}>
          Find speakers
        </TabButton>
        <TabButton active={tab === 'upload'} onClick={() => setTab('upload')}>
          Upload
        </TabButton>
      </div>

      {tab === 'find' && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">Source</span>
              <select
                value={selectedSourceId}
                onChange={(e) => setSelectedSourceId(e.target.value)}
                disabled={scoutableSources.length === 0 || isScanning}
                className="border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm
                  bg-white dark:bg-gray-800 dark:text-gray-200
                  disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {scoutableSources.length === 0 ? (
                  <option value="">No source ready</option>
                ) : (
                  scoutableSources.map((s) => (
                    <option key={s.source_id} value={s.source_id}>
                      {s.filename}
                    </option>
                  ))
                )}
              </select>
            </label>
            <button
              type="button"
              onClick={() => void handleScan()}
              disabled={!selectedSourceId || isScanning}
              className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg
                hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {isScanning ? 'Scanning…' : 'Scan for speakers'}
            </button>
          </div>

          {scout?.status === 'running' && (
            <p className="text-sm text-gray-600 dark:text-gray-400">
              Scanning for speakers…
              {typeof scout.progress === 'number' && (
                <span className="ml-1 font-mono text-gray-500 dark:text-gray-400">{Math.round(scout.progress)}%</span>
              )}
              {pollError && (
                <span className="ml-2 text-amber-600 dark:text-amber-400">Connection lost — retrying…</span>
              )}
            </p>
          )}

          {scout?.status === 'failed' && (
            <p className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded px-3 py-2">
              Scan failed: {scout.error}
              {candidates.length > 0 && ' Speakers from the last successful scan are still available below.'}
            </p>
          )}

          {scout?.status === 'complete' && candidates.length === 0 && (
            <p className="text-sm text-gray-500 dark:text-gray-400">No speakers found in this source.</p>
          )}

          {candidates.length > 0 && (
            <ul className="space-y-3">
              {candidates.map((c) => {
                const tooShort = c.total_secs < MIN_REFERENCE_SECS
                return (
                  <li
                    key={c.speaker_label}
                    className="border border-gray-200 dark:border-gray-700 rounded-lg p-4 flex flex-col gap-3
                      sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="min-w-0">
                      <p className="font-medium text-gray-900 dark:text-gray-100">{c.speaker_label}</p>
                      <p className="text-xs text-gray-500 dark:text-gray-400">
                        {formatDuration(c.total_secs)} · {c.segment_count} segment
                        {c.segment_count === 1 ? '' : 's'}
                      </p>
                    </div>
                    <div className="flex items-center gap-3">
                      <audio
                        controls
                        preload="none"
                        src={getScoutSampleUrl(project.id, c.speaker_label)}
                        className="h-8 max-w-[16rem]"
                      />
                      <button
                        type="button"
                        onClick={() => void handleSelect(c.speaker_label)}
                        disabled={tooShort || selectingLabel != null}
                        title={tooShort ? `Needs at least ${MIN_REFERENCE_SECS}s of talk time` : undefined}
                        className="flex-shrink-0 px-3 py-1.5 bg-blue-600 text-white text-sm font-medium rounded-lg
                          hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                      >
                        {selectingLabel === c.speaker_label ? 'Selecting…' : 'Use this voice'}
                      </button>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}

          {findError && <p className="text-sm text-red-600 dark:text-red-400">{findError}</p>}
        </div>
      )}

      {tab === 'upload' && (
        <div className="space-y-3">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            Upload a clean audio clip of the target speaker to use as the reference.
          </p>
          <button
            type="button"
            onClick={() => uploadProgress == null && uploadInputRef.current?.click()}
            disabled={uploadProgress != null}
            className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg
              hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {uploadProgress != null ? 'Uploading…' : 'Upload reference clip'}
          </button>
          <input
            ref={uploadInputRef}
            type="file"
            accept="audio/*"
            className="hidden"
            onChange={(e) => void handleUpload(e)}
            disabled={uploadProgress != null}
          />
          {uploadProgress != null && (
            <div className="w-full h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
              <div
                className={`h-full bg-blue-600 transition-[width] duration-150 ease-out
                  ${uploadProgress >= 1 ? 'animate-pulse' : ''}`}
                style={{ width: `${Math.round(uploadProgress * 100)}%` }}
              />
            </div>
          )}
          {uploadError && <p className="text-sm text-red-600 dark:text-red-400">{uploadError}</p>}
        </div>
      )}

      {/* Footer — current reference + Continue (both tabs) */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-gray-200 dark:border-gray-700 pt-4">
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {currentReference ?? 'No reference set yet.'}
        </p>
        <div className="flex flex-col items-end gap-1">
          <button
            type="button"
            onClick={() => void handleContinue()}
            disabled={!canContinue || continuing}
            className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg
              hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {continuing ? 'Continuing…' : 'Continue'}
          </button>
          {continueError && <p className="text-sm text-red-600 dark:text-red-400">{continueError}</p>}
        </div>
      </div>
    </div>
  )
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors
        ${active
          ? 'border-blue-600 text-blue-600 dark:border-blue-400 dark:text-blue-400'
          : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'}`}
    >
      {children}
    </button>
  )
}
