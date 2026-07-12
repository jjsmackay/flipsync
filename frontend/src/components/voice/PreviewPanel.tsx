import { useEffect, useRef, useState } from 'react'
import type { Model, PreviewConditioning, CreatePreviewRequest } from '../../types/api'
import { createPreview, getPreviews, getPreviewAudioUrl, ApiError } from '../../api/client'

interface PreviewPanelProps {
  projectId: string
  models: Model[]
}

const TEXT_MAX = 500
const POLL_MS = 3000

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
  disabled: boolean
  disabledReason?: string
}

/** Generate button + poll-to-completion + audio player. Renders no card border of its
 *  own — the parent column supplies the card and any heading/model selector. */
function PreviewColumn({ projectId, text, conditioning, modelId, disabled, disabledReason }: PreviewColumnProps) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState<string | null>(null)
  const [objectUrl, setObjectUrl] = useState<string | null>(null)

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const objectUrlRef = useRef<string | null>(null)
  const mountedRef = useRef(true)

  function clearPolling() {
    if (intervalRef.current) {
      clearInterval(intervalRef.current)
      intervalRef.current = null
    }
  }

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
      clearPolling()
      revokeUrl()
    }
  }, [])

  async function loadAudio(previewId: string) {
    const res = await fetch(getPreviewAudioUrl(projectId, previewId))
    const blob = await res.blob()
    if (!mountedRef.current) return
    revokeUrl()
    const url = URL.createObjectURL(blob)
    objectUrlRef.current = url
    setObjectUrl(url)
    setPhase('ready')
  }

  async function handleGenerate() {
    clearPolling()
    revokeUrl()
    setObjectUrl(null)
    setError(null)
    setPhase('generating')

    const body: CreatePreviewRequest = { text, model_id: modelId, conditioning }
    let previewId: string
    try {
      const res = await createPreview(projectId, body)
      previewId = res.enqueued_job.id
    } catch (err) {
      if (!mountedRef.current) return
      if (err instanceof ApiError) {
        setError(ERROR_MESSAGES[err.error] ?? err.message)
      } else {
        setError(err instanceof Error ? err.message : 'Failed to start synthesis.')
      }
      setPhase('error')
      return
    }

    intervalRef.current = setInterval(() => {
      void (async () => {
        try {
          const { previews } = await getPreviews(projectId)
          if (!mountedRef.current) return
          const preview = previews.find((p) => p.id === previewId)
          if (!preview) return
          if (preview.status === 'complete') {
            clearPolling()
            await loadAudio(previewId)
          } else if (preview.status === 'failed') {
            clearPolling()
            setError('Synthesis failed.')
            setPhase('error')
          }
        } catch {
          /* transient poll error — keep polling */
        }
      })()
    }, POLL_MS)
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
      {disabled && disabledReason && <p className="text-xs text-gray-500">{disabledReason}</p>}
      {error && <p className="text-xs text-red-600">{error}</p>}
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
      <div className="rounded-lg border border-gray-200 bg-white p-4 space-y-3">
        <div>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={TEXT_MAX}
            rows={3}
            placeholder="Text to synthesise…"
            className="w-full rounded border border-gray-300 px-3 py-2 text-sm resize-y"
          />
          <div className="flex justify-end text-xs text-gray-400 mt-1">
            {text.length} / {TEXT_MAX}
          </div>
        </div>
        <label className="flex items-center gap-2 text-sm text-gray-700">
          <span className="w-32">Conditioning</span>
          <select
            value={source}
            onChange={(e) => setSource(e.target.value as ConditioningOption)}
            className="rounded border border-gray-300 px-2 py-1 text-sm"
          >
            {(Object.keys(CONDITIONING_LABELS) as ConditioningOption[]).map((opt) => (
              <option key={opt} value={opt}>
                {CONDITIONING_LABELS[opt]}
              </option>
            ))}
          </select>
        </label>
        <p className="text-xs text-gray-500">
          Generate the same text against the base model and a fine-tuned model to compare by ear.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="rounded-lg border border-gray-200 bg-white p-4 space-y-3">
          <p className="text-sm font-semibold text-gray-700">Zero-shot (base model)</p>
          <PreviewColumn
            projectId={projectId}
            text={trimmed}
            conditioning={conditioning}
            modelId={null}
            disabled={textInvalid}
            disabledReason={textInvalid ? 'Enter text to synthesise.' : undefined}
          />
        </div>

        <div className="rounded-lg border border-gray-200 bg-white p-4 space-y-3">
          <label className="flex items-center gap-2 text-sm">
            <span className="font-semibold text-gray-700">Fine-tuned</span>
            <select
              value={selectedModelId ?? ''}
              onChange={(e) => setSelectedModelId(e.target.value || null)}
              disabled={noModel}
              className="flex-1 rounded border border-gray-300 px-2 py-1 text-sm text-gray-700 disabled:bg-gray-50"
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
