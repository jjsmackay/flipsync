import { useState } from 'react'
import type { Model } from '../../types/api'
import { deleteModel, ApiError } from '../../api/client'
import { formatDuration } from '../../utils/format'
import { StatusBadge } from '../ui/StatusBadge'

interface ModelsListProps {
  projectId: string
  models: Model[]
  loading: boolean
  error: string | null
  /** Reload models after a delete. */
  onChanged: () => void
}

function formatCreated(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

export function ModelsList({ projectId, models, loading, error, onChanged }: ModelsListProps) {
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  async function handleDelete(modelId: string) {
    setDeleteError(null)
    setDeletingId(modelId)
    try {
      await deleteModel(projectId, modelId)
      setConfirmId(null)
      onChanged()
    } catch (err) {
      if (err instanceof ApiError && err.error === 'model_training') {
        setDeleteError('Cannot delete a model while it is training.')
      } else {
        setDeleteError(err instanceof Error ? err.message : 'Delete failed.')
      }
    } finally {
      setDeletingId(null)
    }
  }

  if (loading && models.length === 0) {
    return <p className="text-sm text-gray-500 dark:text-gray-400">Loading models…</p>
  }

  if (error) {
    return <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
  }

  if (models.length === 0) {
    return <p className="text-sm text-gray-500 dark:text-gray-400">No models trained yet.</p>
  }

  return (
    <div className="space-y-2">
      {deleteError && <p className="text-xs text-red-600 dark:text-red-400">{deleteError}</p>}
      {models.map((model) => {
        const training = model.status === 'pending' || model.status === 'training'
        return (
          <div
            key={model.id}
            className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4"
          >
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0 space-y-1">
                <div className="flex items-center gap-2">
                  <StatusBadge status={model.status} kind="model" />
                  <span className="text-sm text-gray-700 dark:text-gray-300">
                    {model.dataset_mode === 'auto'
                      ? `Auto (≥ ${model.min_confidence ?? '—'})`
                      : 'Reviewed'}
                  </span>
                </div>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  {model.dataset_duration_secs != null
                    ? formatDuration(model.dataset_duration_secs)
                    : '—'}{' '}
                  · {model.segment_count ?? '—'} segments
                  {model.eval_loss != null && ` · eval loss ${model.eval_loss.toFixed(4)}`}
                </p>
                <p className="text-xs text-gray-400 dark:text-gray-500">{formatCreated(model.created_at)}</p>
                {model.status === 'failed' && model.error && (
                  <p className="text-xs text-red-600 dark:text-red-400">{model.error}</p>
                )}
              </div>

              <div className="flex-shrink-0">
                {confirmId === model.id ? (
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setConfirmId(null)}
                      className="text-xs px-2 py-1 rounded border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/50"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleDelete(model.id)}
                      disabled={deletingId === model.id}
                      className="text-xs px-2 py-1 rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {deletingId === model.id ? 'Deleting…' : 'Delete'}
                    </button>
                  </div>
                ) : (
                  <button
                    type="button"
                    onClick={() => setConfirmId(model.id)}
                    disabled={training}
                    title={training ? 'Cannot delete while training' : undefined}
                    className="text-xs px-2 py-1 rounded border border-red-200 dark:border-red-800 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    Delete
                  </button>
                )}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}
