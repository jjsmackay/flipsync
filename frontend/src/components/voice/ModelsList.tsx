import { useState } from 'react'
import type { Model, ModelStatus } from '../../types/api'
import { deleteModel, ApiError } from '../../api/client'
import { formatDuration } from '../../utils/format'

interface ModelsListProps {
  projectId: string
  models: Model[]
  loading: boolean
  error: string | null
  /** Reload models after a delete. */
  onChanged: () => void
}

const STATUS_STYLES: Record<ModelStatus, string> = {
  pending: 'bg-gray-100 text-gray-700',
  training: 'bg-blue-100 text-blue-700',
  ready: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  cancelled: 'bg-gray-200 text-gray-500',
}

function ModelStatusBadge({ status }: { status: ModelStatus }) {
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${STATUS_STYLES[status]}`}>
      {status}
    </span>
  )
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
    return <p className="text-sm text-gray-500">Loading models…</p>
  }

  if (error) {
    return <p className="text-sm text-red-600">{error}</p>
  }

  if (models.length === 0) {
    return <p className="text-sm text-gray-500">No models trained yet.</p>
  }

  return (
    <div className="space-y-2">
      {deleteError && <p className="text-xs text-red-600">{deleteError}</p>}
      {models.map((model) => {
        const training = model.status === 'pending' || model.status === 'training'
        return (
          <div key={model.id} className="rounded-lg border border-gray-200 bg-white p-4">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0 space-y-1">
                <div className="flex items-center gap-2">
                  <ModelStatusBadge status={model.status} />
                  <span className="text-sm text-gray-700">
                    {model.dataset_mode === 'auto'
                      ? `Auto (≥ ${model.min_confidence ?? '—'})`
                      : 'Reviewed'}
                  </span>
                </div>
                <p className="text-xs text-gray-500">
                  {model.dataset_duration_secs != null
                    ? formatDuration(model.dataset_duration_secs)
                    : '—'}{' '}
                  · {model.segment_count ?? '—'} segments
                  {model.eval_loss != null && ` · eval loss ${model.eval_loss.toFixed(4)}`}
                </p>
                <p className="text-xs text-gray-400">{formatCreated(model.created_at)}</p>
                {model.status === 'failed' && model.error && (
                  <p className="text-xs text-red-600">{model.error}</p>
                )}
              </div>

              <div className="flex-shrink-0">
                {confirmId === model.id ? (
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setConfirmId(null)}
                      className="text-xs px-2 py-1 rounded border border-gray-300 text-gray-600 hover:bg-gray-50"
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
                    className="text-xs px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-40 disabled:cursor-not-allowed"
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
