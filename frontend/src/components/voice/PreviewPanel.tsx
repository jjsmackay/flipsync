import { useCallback, useEffect, useRef, useState } from 'react'
import type { Model, Preview, PreviewConditioning, CreatePreviewRequest } from '../../types/api'
import { createPreview, deletePreview, getPreviews, getPreviewAudioUrl, getProject, ApiError } from '../../api/client'
import { usePolling } from '../../hooks/usePolling'
import { errorMessage } from '../../utils/errors'
import { SamplingParams, DEFAULT_SAMPLING, SliderRow, CheckboxRow, NumericSamplingKey } from './sampling'
import { PreviewMeta, InlineDelete } from './history'

interface PreviewPanelProps {
  projectId: string
  models: Model[]
  /** Header toggle: show the top-k / top-p dials. Values still ride every
   *  request, so hiding them mid-session loses nothing. */
  advanced?: boolean
  /** Whether the deployment's XTTS engine is healthy. Gates the zero-shot
   *  "base model" column: a base (no model_id) preview is XTTS-only at the
   *  orchestrator, so a GPT-SoVITS-only deployment has no untrained preview
   *  to offer. Defaults true (existing XTTS-only behaviour). */
  xttsAvailable?: boolean
}

const TEXT_MAX = 500
const POLL_MS = 3000

// Bounded polling lifetime: a hung preview job (or an id that never appears in the
// limit-20 previews list) must not spin the poll forever. Synthesis takes seconds;
// ten minutes is generous even behind a queued GPU job.
const PREVIEW_TIMEOUT_MS = 10 * 60_000

// Prefill with a stock sentence so the generate buttons are live on render.
// Without this, users see disabled buttons and think previews are broken.
const DEFAULT_TEXT = "Here's a quick preview of this voice. The quick brown fox jumps over the lazy dog."

type ConditioningOption = 'auto' | 'reference_clip' | 'segments_raw' | 'segments_cleaned'

const CONDITIONING_LABELS: Record<ConditioningOption, string> = {
  auto: 'Auto (best available)',
  reference_clip: 'Reference clip',
  segments_raw: 'Raw segments',
  segments_cleaned: 'Cleaned segments',
}

const ERROR_MESSAGES: Record<string, string> = {
  conditioning_unavailable: 'No audio available for the chosen conditioning source yet.',
  model_not_ready: 'That model is not ready.',
  xtts_unavailable: 'The voice service is not deployed or is unhealthy.',
}

type Phase = 'idle' | 'generating' | 'ready' | 'error'

interface PreviewColumnProps {
  projectId: string
  text: string
  conditioning: PreviewConditioning | undefined
  modelId: string | null
  /** null → send no sampling knobs, letting the engine service apply its own
   *  defaults (GPT-SoVITS models: XTTS numbers would garble the audio). */
  sampling: SamplingParams | null
  disabled: boolean
  disabledReason?: string
  /** Fired when a generated take finishes, so the panel can refresh history. */
  onComplete?: () => void
}

/** Generate button + poll-to-completion + audio player. Renders no card border of its
 *  own — the parent column supplies the card and any heading/model selector. */
function PreviewColumn({ projectId, text, conditioning, modelId, sampling, disabled, disabledReason, onComplete }: PreviewColumnProps) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState<string | null>(null)
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  // The preview id we are waiting on; null when nothing is pending.
  const [previewId, setPreviewId] = useState<string | null>(null)

  const objectUrlRef = useRef<string | null>(null)
  const mountedRef = useRef(true)

  function revokeUrl() {
    if (objectUrlRef.current) {
      URL.revokeObjectURL(objectUrlRef.current)
      objectUrlRef.current = null
    }
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      revokeUrl()
    }
  }, [])

  async function loadAudio(id: string) {
    const res = await fetch(getPreviewAudioUrl(projectId, id))
    // A non-2xx here returns the JSON error body — never hand that to the audio element.
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    if (!mountedRef.current) return
    revokeUrl()
    const url = URL.createObjectURL(blob)
    objectUrlRef.current = url
    setObjectUrl(url)
    setPhase('ready')
    onComplete?.()
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
      if (!previewId) return
      const preview = previews.find((p) => p.id === previewId)
      if (!preview) return
      if (preview.status === 'complete') {
        setPreviewId(null)
        loadAudio(previewId).catch((err: unknown) => {
          if (!mountedRef.current) return
          setError(errorMessage(err, 'Failed to load generated audio.'))
          setPhase('error')
        })
      } else if (preview.status === 'failed') {
        setPreviewId(null)
        void surfaceFailure(previewId)
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

  // Bounded lifetime: give up after PREVIEW_TIMEOUT_MS of generating.
  useEffect(() => {
    if (phase !== 'generating') return
    const timer = setTimeout(() => {
      setPreviewId(null)
      setError('Preview timed out — check failed jobs.')
      setPhase('error')
    }, PREVIEW_TIMEOUT_MS)
    return () => clearTimeout(timer)
  }, [phase])

  async function handleGenerate() {
    revokeUrl()
    setObjectUrl(null)
    setError(null)
    setPreviewId(null)
    setPhase('generating')

    const body: CreatePreviewRequest = { text, model_id: modelId, conditioning, ...(sampling ?? {}) }
    try {
      const res = await createPreview(projectId, body)
      if (!mountedRef.current) return
      setPreviewId(res.enqueued_job.id)
    } catch (err) {
      if (!mountedRef.current) return
      if (err instanceof ApiError) {
        setError(ERROR_MESSAGES[err.error] ?? err.message)
      } else {
        setError(errorMessage(err, 'Failed to start synthesis.'))
      }
      setPhase('error')
    }
  }

  return (
    <div className="space-y-2">
      <button
        type="button"
        onClick={() => void handleGenerate()}
        disabled={disabled || phase === 'generating'}
        title={disabled ? disabledReason : undefined}
        className="px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {phase === 'generating' ? 'Generating…' : 'Generate'}
      </button>
      {disabled && disabledReason && (
        <p className="text-xs text-gray-500 dark:text-gray-400">{disabledReason}</p>
      )}
      {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      {objectUrl && (
        <audio controls src={objectUrl} className="w-full">
          Your browser does not support audio playback.
        </audio>
      )}
    </div>
  )
}

export function PreviewPanel({ projectId, models, advanced = false, xttsAvailable = true }: PreviewPanelProps) {
  const readyModels = models.filter((m) => m.status === 'ready')

  const [text, setText] = useState(DEFAULT_TEXT)
  const [source, setSource] = useState<ConditioningOption>('auto')
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null)
  // Shared across both columns so A/B compares models, not sampling noise.
  const [sampling, setSampling] = useState<SamplingParams>(DEFAULT_SAMPLING)

  function setKnob(key: NumericSamplingKey) {
    return (value: number) => setSampling((prev) => ({ ...prev, [key]: value }))
  }
  function setSplitting(value: boolean) {
    setSampling((prev) => ({ ...prev, enable_text_splitting: value }))
  }

  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  // Free-text preview history (segment-linked takes belong to the Compare panel).
  // Polling is per-column, so a completed take triggers reloadHistory via onComplete.
  const [history, setHistory] = useState<Preview[]>([])
  const reloadHistory = useCallback(
    () =>
      getPreviews(projectId)
        .then((res) => {
          if (!mountedRef.current) return
          setHistory(res.previews.filter((p) => p.segment_id === null))
        })
        .catch(() => {}),
    [projectId],
  )
  useEffect(() => { void reloadHistory() }, [reloadHistory])

  // Default the fine-tuned column to the newest ready model once one exists.
  useEffect(() => {
    if (selectedModelId && readyModels.some((m) => m.id === selectedModelId)) return
    setSelectedModelId(readyModels[0]?.id ?? null)
  }, [readyModels, selectedModelId])

  // The sliders are XTTS dials: a GPT-SoVITS model's preview sends no sampling
  // knobs at all — the service's own defaults apply (XTTS numbers, especially
  // repetition_penalty 10 vs the engine's 2.0 cap, garble its audio).
  const selectedEngine = readyModels.find((m) => m.id === selectedModelId)?.engine ?? 'xtts'
  const fineTunedSampling = selectedEngine === 'gpt_sovits' ? null : sampling
  // Hide the sliders when no visible column uses them (gpt-sovits-only
  // deployment); with the base column present they still drive it.
  const showSliders = xttsAvailable || selectedEngine !== 'gpt_sovits'

  const conditioning: PreviewConditioning | undefined =
    source === 'auto' ? undefined : { source, segment_count: 5 }

  const trimmed = text.trim()
  const textInvalid = trimmed.length === 0 || text.length > TEXT_MAX
  const noModel = readyModels.length === 0

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 space-y-3">
        <div>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={TEXT_MAX}
            rows={3}
            placeholder="Text to synthesise…"
            className="w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-100 px-3 py-2 text-sm resize-y"
          />
          <div className="flex justify-end text-xs text-gray-400 dark:text-gray-500 mt-1">
            {text.length} / {TEXT_MAX}
          </div>
        </div>
        {/* Synthesis parameters — same container treatment as the step settings cards. */}
        <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-4 space-y-3">
          <div>
            <label
              htmlFor="preview-conditioning"
              className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-0.5"
            >
              Conditioning
            </label>
            <select
              id="preview-conditioning"
              value={source}
              onChange={(e) => setSource(e.target.value as ConditioningOption)}
              className="rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-100 px-2 py-1 text-sm"
            >
              {(Object.keys(CONDITIONING_LABELS) as ConditioningOption[]).map((opt) => (
                <option key={opt} value={opt}>
                  {CONDITIONING_LABELS[opt]}
                </option>
              ))}
            </select>
          </div>
          {showSliders && (
          <div className="grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2">
          <SliderRow
            id="preview-temperature"
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
            id="preview-speed"
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
                id="preview-top-k"
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
                id="preview-top-p"
                label="Top-p"
                min={0.05}
                max={1}
                step={0.05}
                decimals={2}
                value={sampling.top_p}
                onChange={setKnob('top_p')}
                hint="Nucleus cutoff. Lower keeps only the most probable continuations."
              />
              <SliderRow
                id="preview-repetition-penalty"
                label="Repetition penalty"
                min={1}
                max={20}
                step={0.5}
                decimals={1}
                value={sampling.repetition_penalty}
                onChange={setKnob('repetition_penalty')}
                hint="Higher discourages repeats — raise to kill stutters or looping."
              />
              <CheckboxRow
                id="preview-text-splitting"
                label="Split long text into sentences"
                checked={sampling.enable_text_splitting}
                onChange={setSplitting}
                hint="On (default) gives each sentence its own prosody. Turn off to feed text unsplit."
              />
            </>
          )}
          </div>
          )}
          {selectedEngine === 'gpt_sovits' && (
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {showSliders
                ? 'The sliders apply to the base model only — GPT-SoVITS models use their own tuned sampling defaults.'
                : 'GPT-SoVITS models use their own tuned sampling defaults.'}
            </p>
          )}
        </div>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          {xttsAvailable
            ? 'Generate the same text against the base model and a fine-tuned model to compare by ear.'
            : 'Generate the text through a trained model to preview it by ear.'}
        </p>
      </div>

      <div className={`grid grid-cols-1 gap-4 ${xttsAvailable ? 'sm:grid-cols-2' : ''}`}>
        {xttsAvailable && (
          <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 space-y-3">
            <p className="text-sm font-semibold text-gray-700 dark:text-gray-300">Zero-shot (base model)</p>
            <PreviewColumn
              projectId={projectId}
              text={trimmed}
              conditioning={conditioning}
              modelId={null}
              sampling={sampling}
              disabled={textInvalid}
              disabledReason={textInvalid ? 'Enter text to synthesise.' : undefined}
              onComplete={reloadHistory}
            />
          </div>
        )}

        <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 space-y-3">
          <label className="flex items-center gap-2 text-sm">
            <span className="font-semibold text-gray-700 dark:text-gray-300">Fine-tuned</span>
            <select
              value={selectedModelId ?? ''}
              onChange={(e) => setSelectedModelId(e.target.value || null)}
              disabled={noModel}
              className="flex-1 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 px-2 py-1 text-sm text-gray-700 dark:text-gray-300 disabled:bg-gray-50 dark:disabled:bg-gray-800"
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
          </label>
          <PreviewColumn
            key={selectedModelId ?? 'none'}
            projectId={projectId}
            text={trimmed}
            conditioning={conditioning}
            modelId={selectedModelId}
            sampling={fineTunedSampling}
            disabled={textInvalid || noModel || selectedModelId === null}
            disabledReason={
              noModel
                ? 'Train a model to enable fine-tuned previews.'
                : textInvalid
                  ? 'Enter text to synthesise.'
                  : undefined
            }
            onComplete={reloadHistory}
          />
        </div>
      </div>

      {history.length > 0 && (
        <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 space-y-2">
          <p className="text-sm font-semibold text-gray-700 dark:text-gray-300">Recent previews</p>
          <ul className="divide-y divide-gray-100 dark:divide-gray-700">
            {history.map((p) => (
              <li key={p.id} className="py-2 space-y-1">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 space-y-1">
                    <p className="text-sm text-gray-700 dark:text-gray-300">{p.text}</p>
                    <PreviewMeta models={models} modelId={p.model_id} sampling={p.sampling} advanced={advanced} />
                  </div>
                  <div className="flex-shrink-0">
                    <InlineDelete onDelete={() => deletePreview(projectId, p.id).then(reloadHistory)} />
                  </div>
                </div>
                {p.status === 'complete' && (
                  <audio controls src={getPreviewAudioUrl(projectId, p.id)} className="w-full" />
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
