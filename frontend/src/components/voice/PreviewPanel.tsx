import { useCallback, useEffect, useRef, useState } from 'react'
import type { Model, Preview, PreviewConditioning, CreatePreviewRequest } from '../../types/api'
import { createPreview, getPreviews, getPreviewAudioUrl, getProject, ApiError } from '../../api/client'
import { usePolling } from '../../hooks/usePolling'
import { errorMessage } from '../../utils/errors'

interface PreviewPanelProps {
  projectId: string
  models: Model[]
}

const TEXT_MAX = 500
const DEFAULT_TEMPERATURE = 0.65
const POLL_MS = 3000
// Bounded polling lifetime: a hung preview job (or an id that never appears in the
// limit-20 previews list) must not spin the poll forever. Synthesis takes seconds;
// ten minutes is generous even behind a queued GPU job.
const PREVIEW_TIMEOUT_MS = 10 * 60_000

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
  temperature: number
  disabled: boolean
  disabledReason?: string
}

/** Generate button + poll-to-completion + audio player. Renders no card border of its
 *  own — the parent column supplies the card and any heading/model selector. */
function PreviewColumn({ projectId, text, conditioning, modelId, temperature, disabled, disabledReason }: PreviewColumnProps) {
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

    const body: CreatePreviewRequest = { text, model_id: modelId, conditioning, temperature }
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

export function PreviewPanel({ projectId, models }: PreviewPanelProps) {
  const readyModels = models.filter((m) => m.status === 'ready')

  const [text, setText] = useState('')
  const [source, setSource] = useState<ConditioningOption>('auto')
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null)
  // Shared across both columns so A/B compares models, not sampling noise.
  const [temperature, setTemperature] = useState(DEFAULT_TEMPERATURE)

  // Default the fine-tuned column to the newest ready model once one exists.
  useEffect(() => {
    if (selectedModelId && readyModels.some((m) => m.id === selectedModelId)) return
    setSelectedModelId(readyModels[0]?.id ?? null)
  }, [readyModels, selectedModelId])

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
        <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
          <span className="w-32">Conditioning</span>
          <select
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
        </label>
        <div className="space-y-1">
          <div className="flex items-baseline justify-between gap-3">
            <label htmlFor="preview-temperature" className="text-sm text-gray-700 dark:text-gray-300">
              Temperature
            </label>
            <span className="shrink-0 font-mono text-blue-600">{temperature.toFixed(2)}</span>
          </div>
          <input
            id="preview-temperature"
            type="range"
            min={0.05}
            max={2}
            step={0.05}
            value={temperature}
            onChange={(e) => setTemperature(parseFloat(e.target.value))}
            className="w-full accent-blue-600"
          />
          <p className="text-xs text-gray-500 dark:text-gray-400">Higher = more varied delivery.</p>
        </div>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Generate the same text against the base model and a fine-tuned model to compare by ear.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 space-y-3">
          <p className="text-sm font-semibold text-gray-700 dark:text-gray-300">Zero-shot (base model)</p>
          <PreviewColumn
            projectId={projectId}
            text={trimmed}
            conditioning={conditioning}
            modelId={null}
            temperature={temperature}
            disabled={textInvalid}
            disabledReason={textInvalid ? 'Enter text to synthesise.' : undefined}
          />
        </div>

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
            temperature={temperature}
            disabled={textInvalid || noModel || selectedModelId === null}
            disabledReason={
              noModel
                ? 'Train a model to enable fine-tuned previews.'
                : textInvalid
                  ? 'Enter text to synthesise.'
                  : undefined
            }
          />
        </div>
      </div>
    </div>
  )
}
