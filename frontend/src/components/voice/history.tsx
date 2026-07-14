// Shared bits for the preview/comparison history lists (ComparePanel and
// PreviewPanel): a provenance line (which model + sampling knobs a take used)
// and an inline confirm-then-delete control.
import { useState } from 'react'
import type { Model, PreviewSampling } from '../../types/api'
import { errorMessage } from '../../utils/errors'

/** Human label for a preview's model_id: null = XTTS base (zero-shot); a live
 *  model shows its mode + short id; a since-deleted model degrades gracefully. */
export function modelLabel(models: Model[], modelId: string | null): string {
  if (!modelId) return 'Base model'
  const m = models.find((x) => x.id === modelId)
  if (!m) return `${modelId.slice(0, 8)} (deleted)`
  return `${m.dataset_mode === 'auto' ? 'Auto' : 'Reviewed'} · ${m.id.slice(0, 8)}`
}

function samplingSummary(s: PreviewSampling | null, advanced: boolean): string {
  if (!s) return ''
  const parts: string[] = []
  if (s.temperature != null) parts.push(`temp ${s.temperature}`)
  if (s.speed != null) parts.push(`speed ${s.speed}`)
  if (advanced && s.top_k != null) parts.push(`top-k ${s.top_k}`)
  if (advanced && s.top_p != null) parts.push(`top-p ${s.top_p}`)
  return parts.join(' · ')
}

/** Provenance line under a history entry: model + sampling knobs used. */
export function PreviewMeta({
  models,
  modelId,
  sampling,
  advanced,
}: {
  models: Model[]
  modelId: string | null
  sampling: PreviewSampling | null
  advanced: boolean
}) {
  const summary = samplingSummary(sampling, advanced)
  return (
    <p className="text-xs text-gray-400 dark:text-gray-500">
      {modelLabel(models, modelId)}
      {summary && ` · ${summary}`}
    </p>
  )
}

/** Confirm-then-delete control. On success the parent is expected to refetch and
 *  drop this row, so the busy state need not be reset. Mirrors ModelsList. */
export function InlineDelete({ onDelete, title }: { onDelete: () => Promise<void>; title?: string }) {
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run() {
    setBusy(true)
    setError(null)
    try {
      await onDelete()
    } catch (err) {
      setError(errorMessage(err, 'Delete failed.'))
      setBusy(false)
      setConfirm(false)
    }
  }

  if (!confirm) {
    return (
      <button
        type="button"
        onClick={() => setConfirm(true)}
        title={title}
        className="text-xs px-2 py-1 rounded border border-red-200 dark:border-red-800 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30"
      >
        Delete
      </button>
    )
  }

  return (
    <div className="flex items-center gap-2">
      {error && <span className="text-xs text-red-600 dark:text-red-400">{error}</span>}
      <button
        type="button"
        onClick={() => setConfirm(false)}
        disabled={busy}
        className="text-xs px-2 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/50"
      >
        Cancel
      </button>
      <button
        type="button"
        onClick={() => void run()}
        disabled={busy}
        className="text-xs px-2 py-1 rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {busy ? 'Deleting…' : 'Delete'}
      </button>
    </div>
  )
}
