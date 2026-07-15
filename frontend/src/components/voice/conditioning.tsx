// Shared conditioning control for the Preview and Compare panels: a source
// selector plus, for the "custom" source, a saved-clip picker + file upload.
// XTTS-only — GPT-SoVITS ignores orchestrator conditioning, so callers hide it.
import { useCallback, useEffect, useState } from 'react'
import type { ConditioningClip, PreviewConditioning } from '../../types/api'
import { listConditioningClips, uploadConditioningClip } from '../../api/client'
import { errorMessage } from '../../utils/errors'

export type ConditioningOption = 'auto' | 'reference_clip' | 'segments_raw' | 'segments_cleaned' | 'custom'

export const CONDITIONING_LABELS: Record<ConditioningOption, string> = {
  auto: 'Auto (best available)',
  reference_clip: 'Reference clip',
  segments_raw: 'Raw segments',
  segments_cleaned: 'Cleaned segments',
  custom: 'Custom clip…',
}

/** The request `conditioning` for a given selection (undefined = auto/default). */
export function toConditioning(source: ConditioningOption, customClipId: string | null): PreviewConditioning | undefined {
  if (source === 'auto') return undefined
  if (source === 'custom') return { source: 'custom', clip_id: customClipId ?? undefined, segment_count: 5 }
  return { source, segment_count: 5 }
}

/** True when 'custom' is chosen but no clip is picked yet — gate the render. */
export const customClipMissing = (source: ConditioningOption, customClipId: string | null): boolean =>
  source === 'custom' && !customClipId

const clipLabel = (c: ConditioningClip): string => `Clip ${c.clip_id.slice(0, 8)} · ${c.duration_secs.toFixed(1)}s`

export function ConditioningField({
  projectId,
  idPrefix,
  source,
  onSourceChange,
  customClipId,
  onCustomClipChange,
}: {
  projectId: string
  /** Unique element-id prefix so two instances (Preview + Compare) don't clash. */
  idPrefix: string
  source: ConditioningOption
  onSourceChange: (s: ConditioningOption) => void
  customClipId: string | null
  onCustomClipChange: (id: string | null) => void
}) {
  const [clips, setClips] = useState<ConditioningClip[]>([])
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [label, setLabel] = useState<string | null>(null)

  const reload = useCallback(() => {
    listConditioningClips(projectId).then((r) => setClips(r.clips)).catch(() => {})
  }, [projectId])
  useEffect(() => { reload() }, [reload])

  async function upload(file: File) {
    setUploading(true)
    setError(null)
    onCustomClipChange(null)
    try {
      const res = await uploadConditioningClip(projectId, file)
      onCustomClipChange(res.clip_id)
      setLabel(file.name)
      reload()
    } catch (err) {
      setError(errorMessage(err, 'Upload failed.'))
    } finally {
      setUploading(false)
    }
  }

  const inputCls =
    'block rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-100 px-2 py-1 text-xs'

  return (
    <div>
      <label htmlFor={`${idPrefix}-conditioning`} className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-0.5">
        Conditioning
      </label>
      <select
        id={`${idPrefix}-conditioning`}
        value={source}
        onChange={(e) => onSourceChange(e.target.value as ConditioningOption)}
        className={inputCls}
      >
        {(Object.keys(CONDITIONING_LABELS) as ConditioningOption[]).map((opt) => (
          <option key={opt} value={opt}>{CONDITIONING_LABELS[opt]}</option>
        ))}
      </select>
      {source === 'custom' && (
        <div className="mt-2 space-y-1">
          {clips.length > 0 && (
            <select
              value={customClipId && clips.some((c) => c.clip_id === customClipId) ? customClipId : ''}
              onChange={(e) => {
                const id = e.target.value || null
                onCustomClipChange(id)
                const c = clips.find((x) => x.clip_id === id)
                setLabel(c ? clipLabel(c) : null)
                setError(null)
              }}
              className={inputCls}
            >
              <option value="">— pick a saved clip —</option>
              {clips.map((c) => (
                <option key={c.clip_id} value={c.clip_id}>{clipLabel(c)}</option>
              ))}
            </select>
          )}
          <input
            type="file"
            accept="audio/*"
            disabled={uploading}
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) void upload(f)
            }}
            className="block text-xs text-gray-600 dark:text-gray-400 file:mr-2 file:rounded file:border-0 file:bg-blue-600 file:px-2 file:py-1 file:text-white hover:file:bg-blue-700 disabled:opacity-50"
          />
          {uploading && <p className="text-xs text-gray-500 dark:text-gray-400">Uploading…</p>}
          {customClipId && label && !uploading && (
            <p className="text-xs text-green-600 dark:text-green-400">Using {label}</p>
          )}
          {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
          <p className="text-xs text-gray-400 dark:text-gray-500">
            A few seconds of clean, expressive speech (XTTS only). Inference-only — doesn't change diarisation.
            Segments sent via “Use as conditioning” in review appear above.
          </p>
        </div>
      )}
    </div>
  )
}
