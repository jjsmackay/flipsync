import { useEffect, useRef, useState, ChangeEvent } from 'react'
import type { ProjectDetail, ScoutStatus, SpeakerCandidate, PoolTurn } from '../../types/api'
import {
  ApiError,
  startPipeline,
  startScout,
  getScoutStatus,
  getScoutSampleUrl,
  selectScoutSpeaker,
  uploadReference,
} from '../../api/client'
import { formatDuration } from '../../utils/format'
import { jobLabel } from '../../utils/labels'
import { usePolling } from '../../hooks/usePolling'
import { ProgressBar } from '../ui/ProgressBar'

// Minimum reference length (seconds) the orchestrator accepts. The card's
// "Use this voice" is disabled below this so the user never hits the 422
// reference_too_short from the select endpoint.
const MIN_REFERENCE_SECS = 5

// Maximum assembled reference length — mirrors the orchestrator's cap so the
// live preview matches what the server will build.
const REFERENCE_MAX_SECS = 30

// Mirror the orchestrator's reference assembly: longest-first, minus excluded,
// up to the 30 s cap. Returns which turn indices land in the reference and the
// resulting length so the card can preview it and gate "Use this voice".
export function referencePlan(
  pool: PoolTurn[],
  excluded: Set<number>,
): { inReference: Set<number>; totalSecs: number } {
  const included = pool
    .filter((t) => !excluded.has(t.index))
    .sort((a, b) => b.duration - a.duration)
  const inReference = new Set<number>()
  let total = 0
  for (const t of included) {
    const remaining = REFERENCE_MAX_SECS - total
    if (remaining <= 0) break
    inReference.add(t.index)
    total += Math.min(t.duration, remaining)
  }
  return { inReference, totalSecs: total }
}

// Sources whose vocals stem is ready — the only ones a scout can run against.
const SCOUTABLE_STATUSES = new Set([
  'diarisation_pending',
  'diarisation_running',
  'diarisation_failed',
  'complete',
])

// The Speaker stage runs as a small state machine. Because the prompt is the
// only trigger for separation (and the upload-clip path sets a reference before
// separation runs), this whole component only ever renders while no reference
// is set — deriveStage guarantees it.
type Phase = 'preparing' | 'prompt' | 'separating' | 'failed' | 'scan'

interface SetReferencePanelProps {
  project: ProjectDetail
  onAction: () => void
  pollIntervalMs?: number
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback
}

function derivePhase(project: ProjectDetail): Phase {
  const sources = project.stats.source_coverage
  // A source with a ready vocals stem — scan and pick a voice.
  if (sources.some((s) => SCOUTABLE_STATUSES.has(s.status))) return 'scan'
  // Separation running for the scan.
  if (
    project.active_jobs.some((j) => j.type === 'vocal_separation') ||
    sources.some((s) => s.status === 'separation_running')
  ) {
    return 'separating'
  }
  // A step failed before we got a reference — the FailedJobsPanel owns retry.
  if (sources.some((s) => s.status === 'extraction_failed' || s.status === 'separation_failed')) {
    return 'failed'
  }
  // Audio extracted, ready to ask whose voice we're after.
  if (sources.some((s) => s.status === 'separation_pending')) return 'prompt'
  // Still uploading / extracting.
  return 'preparing'
}

function Heading({ title, blurb }: { title: string; blurb?: string }) {
  return (
    <div className="mb-4">
      <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">{title}</h3>
      {blurb && <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">{blurb}</p>}
    </div>
  )
}

export function SetReferencePanel({ project, onAction, pollIntervalMs = 3000 }: SetReferencePanelProps) {
  const phase = derivePhase(project)

  const scoutable = project.stats.source_coverage.filter((s) => SCOUTABLE_STATUSES.has(s.status))
  const autoSourceId = scoutable[0]?.source_id ?? ''

  const [scout, setScout] = useState<ScoutStatus | null>(null)
  const [scoutLoaded, setScoutLoaded] = useState(false)
  const [selectingLabel, setSelectingLabel] = useState<string | null>(null)
  const [findError, setFindError] = useState<string | null>(null)
  const autoFired = useRef(false)

  // Per-speaker excluded pool indices, which cards are expanded for curation,
  // and the optional expected-speaker-count for the next scan.
  const [excluded, setExcluded] = useState<Record<string, Set<number>>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [expectedCount, setExpectedCount] = useState('')

  function toggleExcluded(label: string, index: number) {
    setExcluded((prev) => {
      const next = new Set(prev[label] ?? [])
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return { ...prev, [label]: next }
    })
  }

  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)

  const [uploadProgress, setUploadProgress] = useState<number | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const uploadInputRef = useRef<HTMLInputElement>(null)

  // Fetch any existing scout results once on mount so a prior scan's candidates
  // (or an in-flight scan) show without re-scanning. A never-run scout 404s
  // with no_scout — that's expected, not an error, and lets the auto-scan fire.
  useEffect(() => {
    let cancelled = false
    getScoutStatus(project.id)
      .then((result) => {
        if (cancelled) return
        setScout(result)
      })
      .catch((err) => {
        if (cancelled) return
        if (!(err instanceof ApiError && err.error === 'no_scout')) {
          setFindError(errorMessage(err, 'Failed to load scout status'))
        }
      })
      .finally(() => {
        if (!cancelled) setScoutLoaded(true)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id])

  async function beginScan(sourceId: string, expected?: number) {
    setFindError(null)
    try {
      await startScout(project.id, sourceId, expected)
      // A new scan supersedes prior curation state.
      setExcluded({})
      setExpanded({})
      // Setting a running status starts the poll loop (derived from scout state).
      setScout({ status: 'running', progress: 0, source_id: sourceId, speakers: [] })
    } catch (err) {
      setFindError(errorMessage(err, 'Failed to start scan'))
    }
  }

  // Auto-scan the first ready source once we know no scan exists yet. Fires
  // exactly once per mount; a manual "Scan again" goes through beginScan too.
  useEffect(() => {
    if (phase !== 'scan' || !scoutLoaded || scout != null || !autoSourceId) return
    if (autoFired.current) return
    autoFired.current = true
    void beginScan(autoSourceId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, scoutLoaded, scout, autoSourceId])

  // Poll scout status while a scan is running — derived purely from fetched
  // state, so one failed poll can't wedge the panel: usePolling keeps its
  // interval alive through errors (we surface a transient "retrying" note) and
  // the loop stops itself when the fetched status leaves 'running'.
  const scoutRunning = scout?.status === 'running'
  const { error: pollError } = usePolling<ScoutStatus>(() => getScoutStatus(project.id), {
    intervalMs: pollIntervalMs,
    enabled: scoutRunning,
    onData: setScout,
  })

  async function handleFindSpeakers() {
    setStartError(null)
    setStarting(true)
    try {
      await startPipeline(project.id)
      onAction()
    } catch (err) {
      setStartError(errorMessage(err, 'Failed to start processing'))
    } finally {
      setStarting(false)
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

  async function handleSelect(label: string, excludedIndices: number[]) {
    setFindError(null)
    setSelectingLabel(label)
    try {
      await selectScoutSpeaker(project.id, label, excludedIndices)
      onAction()
    } catch (err) {
      setFindError(errorMessage(err, 'Failed to select speaker'))
    } finally {
      setSelectingLabel(null)
    }
  }

  const separationJob = project.active_jobs.find((j) => j.type === 'vocal_separation')

  if (phase === 'preparing') {
    return (
      <div>
        <Heading title="Getting your video ready" blurb="Extracting the audio so we can isolate the vocals." />
        <p className="text-sm text-gray-600 dark:text-gray-400">This only takes a moment…</p>
      </div>
    )
  }

  if (phase === 'failed') {
    return (
      <div>
        <Heading
          title="Something went wrong"
          blurb="Preparing your video failed — retry it from the alert below."
        />
      </div>
    )
  }

  if (phase === 'separating') {
    return (
      <div>
        <Heading
          title="Finding the speakers"
          blurb="Isolating the vocals so we can scan for voices. This can take a while — you can leave this page."
        />
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm font-medium text-blue-800 dark:text-blue-300">
              {jobLabel('vocal_separation')}
            </span>
            {separationJob?.status && (
              <span className="text-xs text-blue-500 dark:text-blue-400 capitalize">{separationJob.status}</span>
            )}
          </div>
          {separationJob?.progress != null && <ProgressBar value={separationJob.progress} color="blue" />}
        </div>
      </div>
    )
  }

  if (phase === 'prompt') {
    return (
      <div>
        <Heading
          title="Whose voice are we after?"
          blurb="Scan your video for the speakers in it, or upload a short clean clip of the target voice."
        />
        <div className="flex flex-wrap gap-3">
          <button
            type="button"
            onClick={() => void handleFindSpeakers()}
            disabled={starting || uploadProgress != null}
            className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg
              hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {starting ? 'Starting…' : 'Find speakers'}
          </button>
          <button
            type="button"
            onClick={() => uploadProgress == null && uploadInputRef.current?.click()}
            disabled={starting || uploadProgress != null}
            className="px-4 py-2 border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 text-sm font-medium rounded-lg
              hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {uploadProgress != null ? 'Uploading…' : 'Upload a clip'}
          </button>
          <input
            ref={uploadInputRef}
            type="file"
            accept="audio/*"
            className="hidden"
            onChange={(e) => void handleUpload(e)}
            disabled={uploadProgress != null}
          />
        </div>
        {uploadProgress != null && (
          <div className="mt-3 w-full h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
            <div
              className={`h-full bg-blue-600 transition-[width] duration-150 ease-out
                ${uploadProgress >= 1 ? 'animate-pulse' : ''}`}
              style={{ width: `${Math.round(uploadProgress * 100)}%` }}
            />
          </div>
        )}
        {startError && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{startError}</p>}
        {uploadError && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{uploadError}</p>}
      </div>
    )
  }

  // phase === 'scan'
  const candidates: SpeakerCandidate[] =
    scout && scout.speakers ? [...scout.speakers].sort((a, b) => b.total_secs - a.total_secs) : []
  const isScanning = scoutRunning || (!scoutLoaded && scout == null)
  const canRescan = !!autoSourceId && !scoutRunning

  return (
    <div>
      <Heading
        title="Whose voice are we after?"
        blurb="We scanned your video for voices — pick the target speaker below, or upload a clip instead."
      />
      <div className="space-y-4">
        {isScanning && (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            Scanning for speakers…
            {scout?.status === 'running' && typeof scout.progress === 'number' && (
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
            {candidates.map((c) => (
              <CandidateCard
                key={c.speaker_label}
                projectId={project.id}
                candidate={c}
                excludedSet={excluded[c.speaker_label] ?? EMPTY_SET}
                expanded={!!expanded[c.speaker_label]}
                selecting={selectingLabel === c.speaker_label}
                anySelecting={selectingLabel != null}
                onToggleExpand={() =>
                  setExpanded((p) => ({ ...p, [c.speaker_label]: !p[c.speaker_label] }))
                }
                onToggleExclude={toggleExcluded}
                onSelect={handleSelect}
              />
            ))}
          </ul>
        )}

        {(scout?.status === 'complete' || scout?.status === 'failed') && canRescan && (
          <div className="space-y-3">
            <details className="text-sm">
              <summary className="cursor-pointer text-gray-500 dark:text-gray-400 select-none">
                Advanced
              </summary>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <label htmlFor="expected-count" className="text-gray-600 dark:text-gray-400">
                  Expected number of speakers
                </label>
                <input
                  id="expected-count"
                  type="number"
                  min={1}
                  value={expectedCount}
                  onChange={(e) => setExpectedCount(e.target.value)}
                  placeholder="auto"
                  className="w-20 px-2 py-1 rounded border border-gray-300 dark:border-gray-600
                    bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                />
                <span className="text-xs text-gray-400 dark:text-gray-500">applied on the next scan</span>
              </div>
            </details>

            <button
              type="button"
              onClick={() => void beginScan(autoSourceId, parseExpectedCount(expectedCount))}
              className="text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline"
            >
              Scan again
            </button>
          </div>
        )}

        {findError && <p className="text-sm text-red-600 dark:text-red-400">{findError}</p>}
      </div>
    </div>
  )
}

// Stable empty set so a candidate with no exclusions doesn't churn identity.
const EMPTY_SET: Set<number> = new Set()

function parseExpectedCount(value: string): number | undefined {
  const n = parseInt(value, 10)
  return Number.isFinite(n) && n >= 1 ? n : undefined
}

interface CandidateCardProps {
  projectId: string
  candidate: SpeakerCandidate
  excludedSet: Set<number>
  expanded: boolean
  selecting: boolean
  anySelecting: boolean
  onToggleExpand: () => void
  onToggleExclude: (label: string, index: number) => void
  onSelect: (label: string, excludedIndices: number[]) => void
}

function CandidateCard({
  projectId,
  candidate,
  excludedSet,
  expanded,
  selecting,
  anySelecting,
  onToggleExpand,
  onToggleExclude,
  onSelect,
}: CandidateCardProps) {
  const { inReference, totalSecs } = referencePlan(candidate.pool, excludedSet)
  const tooShort = totalSecs < MIN_REFERENCE_SECS

  return (
    <li className="border border-gray-200 dark:border-gray-700 rounded-lg p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="font-medium text-gray-900 dark:text-gray-100">{candidate.speaker_label}</p>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {formatDuration(candidate.total_secs)} · {candidate.segment_count} segment
            {candidate.segment_count === 1 ? '' : 's'}
          </p>
          <p
            className={`text-xs mt-0.5 ${
              tooShort ? 'text-red-600 dark:text-red-400' : 'text-gray-500 dark:text-gray-400'
            }`}
          >
            Reference: {formatDuration(totalSecs)}
            {tooShort && ` · needs ${MIN_REFERENCE_SECS}s+`}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onToggleExpand}
            aria-expanded={expanded}
            className="flex-shrink-0 text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline"
          >
            {expanded ? 'Hide segments' : 'Choose segments'}
          </button>
          <button
            type="button"
            onClick={() => onSelect(candidate.speaker_label, Array.from(excludedSet))}
            disabled={tooShort || anySelecting}
            title={tooShort ? `Needs at least ${MIN_REFERENCE_SECS}s of clean audio` : undefined}
            className="flex-shrink-0 px-3 py-1.5 bg-blue-600 text-white text-sm font-medium rounded-lg
              hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {selecting ? 'Selecting…' : 'Use this voice'}
          </button>
        </div>
      </div>

      {expanded && (
        <ul className="mt-4 space-y-2 border-t border-gray-100 dark:border-gray-800 pt-3">
          {candidate.pool.map((t) => {
            const isExcluded = excludedSet.has(t.index)
            const inRef = inReference.has(t.index)
            const state = isExcluded ? 'Excluded' : inRef ? 'In reference' : 'Backup'
            return (
              <li key={t.index} className="flex items-center gap-3 text-sm">
                <span
                  className={`inline-block w-24 shrink-0 text-xs font-medium ${
                    isExcluded
                      ? 'text-gray-400 line-through'
                      : inRef
                        ? 'text-green-700 dark:text-green-400'
                        : 'text-gray-500 dark:text-gray-400'
                  }`}
                >
                  {state}
                </span>
                <span className="w-14 shrink-0 tabular-nums text-gray-600 dark:text-gray-400">
                  {formatDuration(t.duration)}
                </span>
                <audio
                  controls
                  preload="none"
                  src={getScoutSampleUrl(projectId, candidate.speaker_label, t.index)}
                  className="h-8 flex-1 min-w-0 max-w-[14rem]"
                />
                <label className="flex items-center gap-1.5 shrink-0 text-xs text-gray-600 dark:text-gray-400 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={isExcluded}
                    onChange={() => onToggleExclude(candidate.speaker_label, t.index)}
                  />
                  Not this speaker
                </label>
              </li>
            )
          })}
        </ul>
      )}
    </li>
  )
}
