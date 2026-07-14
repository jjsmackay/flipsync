import { useCallback, useEffect, useRef, useState } from 'react'
import type { Model, Preview, Segment } from '../../types/api'
import {
  createPreview, getPreviews, getPreviewAudioUrl, getSegments,
  getSegmentAudioUrl, getProject, ApiError,
} from '../../api/client'
import { usePolling } from '../../hooks/usePolling'
import { errorMessage } from '../../utils/errors'
import { SamplingParams, DEFAULT_SAMPLING, SliderRow } from './sampling'

interface ComparePanelProps {
  projectId: string
  models: Model[]
  /** Header toggle: show the top-k / top-p dials. Values still ride every
   *  request, so hiding them mid-session loses nothing. */
  advanced?: boolean
}

const POLL_MS = 3000

// Bounded polling lifetime — mirrors PreviewPanel's PREVIEW_TIMEOUT_MS.
const COMPARE_TIMEOUT_MS = 10 * 60_000
const SEARCH_DEBOUNCE_MS = 300

type Phase = 'idle' | 'generating' | 'ready' | 'error'

function effectiveTranscript(seg: Segment): string {
  return seg.transcript_edited ?? seg.transcript ?? ''
}

export function ComparePanel({ projectId, models, advanced = false }: ComparePanelProps) {
  const readyModels = models.filter((m) => m.status === 'ready')

  // --- segment picker ---
  const [query, setQuery] = useState('')
  const [segments, setSegments] = useState<Segment[]>([])
  const [selected, setSelected] = useState<Segment | null>(null)

  useEffect(() => {
    const ctrl = new AbortController()
    const timer = setTimeout(() => {
      getSegments(projectId, {
        status: 'approved,auto_approved',
        ...(query ? { q: query } : {}),
        sort: 'duration',
        order: 'desc',
        per_page: 50,
      }, ctrl.signal)
        .then((res) => setSegments(res.segments))
        .catch(() => { /* aborted or transient — keep the previous list */ })
    }, SEARCH_DEBOUNCE_MS)
    return () => { clearTimeout(timer); ctrl.abort() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, query])

  // --- model + sampling ---
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null)
  useEffect(() => {
    if (selectedModelId && readyModels.some((m) => m.id === selectedModelId)) return
    setSelectedModelId(readyModels[0]?.id ?? null)
  }, [readyModels, selectedModelId])
  const [sampling, setSampling] = useState<SamplingParams>(DEFAULT_SAMPLING)

  function setKnob(key: keyof SamplingParams) {
    return (value: number) => setSampling((prev) => ({ ...prev, [key]: value }))
  }

  // The sliders are XTTS dials: a GPT-SoVITS model's compare sends no sampling
  // knobs at all — the service's own defaults apply (XTTS numbers, especially
  // repetition_penalty 10 vs the engine's 2.0 cap, garble its audio) — so the
  // sliders are hidden rather than shown doing nothing.
  const selectedEngine = readyModels.find((m) => m.id === selectedModelId)?.engine ?? 'xtts'
  const showSliders = selectedEngine !== 'gpt_sovits'

  // --- generate / poll (same shape as PreviewPanel's PreviewColumn) ---
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState<string | null>(null)
  const [previewId, setPreviewId] = useState<string | null>(null)
  const [cloneUrl, setCloneUrl] = useState<string | null>(null)
  const [history, setHistory] = useState<Preview[]>([])
  const cloneUrlRef = useRef<string | null>(null)
  const mountedRef = useRef(true)

  function revokeUrl() {
    if (cloneUrlRef.current) {
      URL.revokeObjectURL(cloneUrlRef.current)
      cloneUrlRef.current = null
    }
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      revokeUrl()
    }
  }, [])

  async function loadClone(id: string) {
    const res = await fetch(getPreviewAudioUrl(projectId, id))
    // A non-2xx here returns the JSON error body — never hand that to the audio element.
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    if (!mountedRef.current) return
    revokeUrl()
    const url = URL.createObjectURL(blob)
    cloneUrlRef.current = url
    setCloneUrl(url)
    setPhase('ready')
  }

  /** The previews list carries no error detail for failed jobs; the failed job row on
   *  the project does — fetch it once to show the real message. */
  async function surfaceFailure(id: string) {
    let message = 'Synthesis failed.'
    try {
      const project = await getProject(projectId)
      const jobError = project.recent_failed_jobs.find((j) => j.id === id)?.error
      if (jobError) message = `Synthesis failed: ${jobError}`
    } catch {
      /* keep the generic message */
    }
    if (!mountedRef.current) return
    setError(message)
    setPhase('error')
  }

  const fetchPreviews = useCallback(() => getPreviews(projectId), [projectId])

  const handlePreviews = useCallback(
    ({ previews }: { previews: Preview[] }) => {
      const filtered = previews.filter((p) => p.segment_id !== null)
      // Cheap guard: skip the setState (and the re-render it causes) when a poll
      // tick's history is unchanged from what's already rendered — only ids and
      // statuses matter for that comparison.
      setHistory((prev) => {
        if (
          prev.length === filtered.length
          && prev.every((p, i) => p.id === filtered[i].id && p.status === filtered[i].status)
        ) {
          return prev
        }
        return filtered
      })
      if (!previewId) return
      const preview = previews.find((p) => p.id === previewId)
      if (!preview) return
      if (preview.status === 'complete') {
        setPreviewId(null)
        loadClone(preview.id).catch((err: unknown) => {
          if (!mountedRef.current) return
          setError(errorMessage(err, 'Failed to load generated audio.'))
          setPhase('error')
        })
      } else if (preview.status === 'failed') {
        setPreviewId(null)
        void surfaceFailure(preview.id)
      }
    },
    [previewId, projectId],
  )

  // usePolling supplies the in-flight guard (a slow response can't stack requests)
  // and stops as soon as the phase leaves 'generating' or the id is resolved.
  usePolling(fetchPreviews, {
    intervalMs: POLL_MS,
    enabled: phase === 'generating' && previewId !== null,
    onData: handlePreviews,
  })

  // Initial history load — independent of the segment picker so a transient
  // failure fetching segments can't silently strand "Past comparisons" empty.
  useEffect(() => {
    getPreviews(projectId)
      .then((res) => {
        if (!mountedRef.current) return
        setHistory(res.previews.filter((p) => p.segment_id !== null))
      })
      .catch(() => {})
  }, [projectId])

  // Bounded lifetime: give up after COMPARE_TIMEOUT_MS of generating.
  useEffect(() => {
    if (phase !== 'generating') return
    const timer = setTimeout(() => {
      setPreviewId(null)
      setError('Preview timed out — check failed jobs.')
      setPhase('error')
    }, COMPARE_TIMEOUT_MS)
    return () => clearTimeout(timer)
  }, [phase])

  async function handleGenerate() {
    if (!selected) return
    revokeUrl()
    setCloneUrl(null)
    setError(null)
    setPreviewId(null)
    setPhase('generating')

    try {
      const res = await createPreview(projectId, {
        segment_id: selected.id,
        model_id: selectedModelId,
        ...(selectedEngine === 'gpt_sovits' ? {} : sampling),
      })
      if (!mountedRef.current) return
      setPreviewId(res.enqueued_job.id)
    } catch (err) {
      if (!mountedRef.current) return
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError(errorMessage(err, 'Failed to start synthesis.'))
      }
      setPhase('error')
    }
  }

  const noModel = readyModels.length === 0
  const canGenerate = selected !== null && !noModel && selectedModelId !== null && phase !== 'generating'

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 space-y-3">
        <div>
          <label
            htmlFor="compare-search"
            className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-0.5"
          >
            Segment
          </label>
          <input
            id="compare-search"
            type="search"
            placeholder="Search transcripts…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-100 px-3 py-2 text-sm"
          />
          <ul className="mt-2 max-h-40 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-700 rounded border border-gray-200 dark:border-gray-700">
            {segments.length === 0 && (
              <li className="px-3 py-2 text-xs text-gray-500 dark:text-gray-400">No approved segments found.</li>
            )}
            {segments.map((s) => (
              <li key={s.id}>
                <button
                  type="button"
                  onClick={() => setSelected(s)}
                  aria-pressed={selected?.id === s.id}
                  className={`w-full text-left px-3 py-2 text-sm ${
                    selected?.id === s.id
                      ? 'bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300'
                      : 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700'
                  }`}
                >
                  {effectiveTranscript(s)}{' '}
                  <span className="text-gray-400 dark:text-gray-500">({s.duration_secs.toFixed(1)}s)</span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        {/* Synthesis parameters — same container treatment as the step settings cards. */}
        <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-4 space-y-3">
          <div>
            <label
              htmlFor="compare-model"
              className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-0.5"
            >
              Model
            </label>
            <select
              id="compare-model"
              value={selectedModelId ?? ''}
              onChange={(e) => setSelectedModelId(e.target.value || null)}
              disabled={noModel}
              className="rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 px-2 py-1 text-sm text-gray-700 dark:text-gray-300 disabled:bg-gray-50 dark:disabled:bg-gray-800"
            >
              {noModel ? (
                <option value="">No ready models</option>
              ) : (
                readyModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.dataset_mode === 'auto' ? 'Auto' : 'Reviewed'} · {m.id.slice(0, 8)}
                  </option>
                ))
              )}
            </select>
          </div>
          {showSliders && (
          <div className="grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2">
            <SliderRow
              id="compare-temperature"
              label="Temperature"
              min={0.05}
              max={2}
              step={0.05}
              decimals={2}
              value={sampling.temperature}
              onChange={setKnob('temperature')}
              hint="Higher = more varied delivery."
            />
            <SliderRow
              id="compare-speed"
              label="Speed"
              min={0.5}
              max={2}
              step={0.05}
              decimals={2}
              value={sampling.speed}
              onChange={setKnob('speed')}
              hint="Speaking-rate multiplier. 1 is the model's natural pace."
            />
            {advanced && (
              <>
                <SliderRow
                  id="compare-top-k"
                  label="Top-k"
                  min={1}
                  max={100}
                  step={1}
                  decimals={0}
                  value={sampling.top_k}
                  onChange={setKnob('top_k')}
                  hint="Samples from only the k most likely tokens."
                />
                <SliderRow
                  id="compare-top-p"
                  label="Top-p"
                  min={0.05}
                  max={1}
                  step={0.05}
                  decimals={2}
                  value={sampling.top_p}
                  onChange={setKnob('top_p')}
                  hint="Nucleus cutoff. Lower keeps only the most probable continuations."
                />
              </>
            )}
          </div>
          )}
          {!showSliders && (
            <p className="text-xs text-gray-500 dark:text-gray-400">
              GPT-SoVITS models use their own tuned sampling defaults.
            </p>
          )}
        </div>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Synthesise the segment's exact transcript through the model and compare it against the original recording.
        </p>

        <div className="space-y-2">
          <button
            type="button"
            onClick={() => void handleGenerate()}
            disabled={!canGenerate}
            title={!selected ? 'Pick a segment to compare.' : noModel ? 'Train a model to enable comparisons.' : undefined}
            className="px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {phase === 'generating' ? 'Generating…' : 'Generate comparison'}
          </button>
          {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
        </div>
      </div>

      {selected && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 space-y-2">
            <p className="text-sm font-semibold text-gray-700 dark:text-gray-300">Original</p>
            <p className="text-xs text-gray-500 dark:text-gray-400">{effectiveTranscript(selected)}</p>
            <audio controls src={getSegmentAudioUrl(projectId, selected.id)} className="w-full">
              Your browser does not support audio playback.
            </audio>
          </div>
          <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 space-y-2">
            <p className="text-sm font-semibold text-gray-700 dark:text-gray-300">Clone</p>
            {cloneUrl ? (
              <audio controls src={cloneUrl} className="w-full">
                Your browser does not support audio playback.
              </audio>
            ) : (
              <p className="text-xs text-gray-500 dark:text-gray-400">Generate a comparison to hear the clone.</p>
            )}
          </div>
        </div>
      )}

      {history.length > 0 && (
        <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 space-y-2">
          <p className="text-sm font-semibold text-gray-700 dark:text-gray-300">Past comparisons</p>
          <ul className="divide-y divide-gray-100 dark:divide-gray-700">
            {history.map((p) => (
              <li key={p.id} className="py-2 space-y-1">
                <p className="text-sm text-gray-700 dark:text-gray-300">{p.text}</p>
                {p.status === 'complete' && (
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <audio
                      controls
                      src={getSegmentAudioUrl(projectId, p.segment_id!)}
                      className="w-full"
                      onError={(e) => { (e.target as HTMLAudioElement).style.display = 'none' }}
                    />
                    <audio controls src={getPreviewAudioUrl(projectId, p.id)} className="w-full" />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
