import { useState } from 'react'
import type { ProjectDetail, Model, CreateModelRequest, JobSummary } from '../../types/api'
import { createModel, ApiError } from '../../api/client'
import { formatDuration } from '../../utils/format'
import { ProgressBar } from '../ui/ProgressBar'

interface TrainPanelProps {
  project: ProjectDetail
  models: Model[]
  /** Called after a train is successfully enqueued so the parent can refetch + reload models. */
  onStarted: () => void
}

// Training-specific thresholds (distinct from the project's export target):
// under 300 s the orchestrator rejects the dataset (insufficient_dataset); 1800 s
// (30 min) is the recommended floor for a usable fine-tune.
const TRAIN_MIN_SECS = 300
const TRAIN_TARGET_SECS = 1800
const DEFAULT_MIN_CONFIDENCE = 0.85

type TrainMode = 'approved' | 'auto'

/** m:ss, matching the gating-reason wording ("4:37 of 5:00 minimum"). */
function mmss(secs: number): string {
  const m = Math.floor(secs / 60)
  const s = Math.floor(secs % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

const ERROR_MESSAGES: Record<string, string> = {
  insufficient_dataset: 'Not enough usable audio to train (300 s minimum after filtering).',
  finetune_in_progress: 'A model for this project is already training. Wait for it to finish.',
  xtts_unavailable: 'The voice service is not deployed or is unhealthy.',
}

function TrainingProgressCard({ job }: { job: JobSummary }) {
  const detail = job.type === 'finetune' ? job.progress_detail : null

  return (
    <div className="rounded-lg border border-blue-200 bg-blue-50 p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-semibold text-blue-800">
          {job.type === 'finetune' ? 'Fine-tuning' : 'Building dataset'}
        </span>
        <span className="text-xs text-blue-500 capitalize">{detail?.phase ?? job.status}</span>
      </div>
      <ProgressBar value={job.progress ?? 0} color="blue" />
      {detail && (
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-blue-800 sm:grid-cols-4">
          <div>
            <dt className="opacity-60">Epoch</dt>
            <dd className="font-medium">{detail.epoch} / {detail.total_epochs}</dd>
          </div>
          <div>
            <dt className="opacity-60">Step</dt>
            <dd className="font-medium">{detail.step} / {detail.total_steps}</dd>
          </div>
          <div>
            <dt className="opacity-60">Train loss</dt>
            <dd className="font-medium">{detail.train_loss?.toFixed(4) ?? '—'}</dd>
          </div>
          <div>
            <dt className="opacity-60">Eval loss</dt>
            <dd className="font-medium">{detail.eval_loss?.toFixed(4) ?? '—'}</dd>
          </div>
          {detail.eta_secs != null && (
            <div className="col-span-2 sm:col-span-4">
              <dt className="opacity-60">ETA</dt>
              <dd className="font-medium">{formatDuration(detail.eta_secs)}</dd>
            </div>
          )}
        </dl>
      )}
    </div>
  )
}

export function TrainPanel({ project, models, onStarted }: TrainPanelProps) {
  const [confirming, setConfirming] = useState(false)
  const [mode, setMode] = useState<TrainMode>('approved')
  const [minConfidence, setMinConfidence] = useState(DEFAULT_MIN_CONFIDENCE)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const approvedDuration = project.stats.approved_duration_secs
  const progressValue = (approvedDuration / TRAIN_TARGET_SECS) * 100

  const activeJob =
    project.active_jobs.find((j) => j.type === 'finetune') ??
    project.active_jobs.find((j) => j.type === 'dataset_build')
  const hasInProgressModel = models.some((m) => m.status === 'pending' || m.status === 'training')
  const busy = Boolean(activeJob) || hasInProgressModel

  const reviewedBlocked = approvedDuration < TRAIN_MIN_SECS
  const belowTarget = approvedDuration < TRAIN_TARGET_SECS
  const startDisabled = submitting || (mode === 'approved' && reviewedBlocked)

  async function handleTrain() {
    setError(null)
    setSubmitting(true)
    const body: CreateModelRequest =
      mode === 'auto'
        ? { dataset: { mode: 'auto', min_confidence: minConfidence } }
        : { dataset: { mode: 'approved' } }
    try {
      await createModel(project.id, body)
      setConfirming(false)
      onStarted()
    } catch (err) {
      if (err instanceof ApiError) {
        setError(ERROR_MESSAGES[err.error] ?? err.message)
      } else {
        setError(err instanceof Error ? err.message : 'Failed to start training.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  // While a dataset build or fine-tune is running, the progress card replaces the
  // train affordance — a second run is rejected server-side (finetune_in_progress).
  if (busy) {
    return activeJob ? (
      <TrainingProgressCard job={activeJob} />
    ) : (
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-800">
        Training queued…
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 space-y-3">
      <div>
        <p className="text-sm font-medium text-gray-700 mb-2">Approved audio for training</p>
        <ProgressBar
          value={progressValue}
          label={`${formatDuration(approvedDuration)} / ${formatDuration(TRAIN_TARGET_SECS)}`}
          color="green"
        />
      </div>

      {!confirming ? (
        <button
          type="button"
          onClick={() => setConfirming(true)}
          className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
        >
          Train voice model
        </button>
      ) : (
        <div className="space-y-3 border-t border-gray-100 pt-3">
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium text-gray-700 mb-1">Training data</legend>
            <label className="flex items-start gap-2 text-sm text-gray-700">
              <input
                type="radio"
                name="train-mode"
                checked={mode === 'approved'}
                onChange={() => setMode('approved')}
                className="mt-0.5"
              />
              <span>
                <span className="font-medium">Reviewed</span>
                <span className="block text-xs text-gray-500">
                  Only segments you have approved.
                </span>
              </span>
            </label>
            <label className="flex items-start gap-2 text-sm text-gray-700">
              <input
                type="radio"
                name="train-mode"
                checked={mode === 'auto'}
                onChange={() => setMode('auto')}
                className="mt-0.5"
              />
              <span>
                <span className="font-medium">Train without review</span>
                <span className="block text-xs text-amber-600">
                  Trades quality for speed — uses unreviewed high-confidence segments.
                  Wrong transcripts degrade the model.
                </span>
              </span>
            </label>
          </fieldset>

          {mode === 'auto' && (
            <label className="flex items-center gap-2 text-sm text-gray-700">
              <span className="w-32">Confidence floor</span>
              <input
                type="number"
                min={0}
                max={1}
                step={0.01}
                value={minConfidence}
                onChange={(e) => setMinConfidence(Number(e.target.value))}
                className="w-24 rounded border border-gray-300 px-2 py-1 text-sm"
              />
            </label>
          )}

          {mode === 'approved' && belowTarget && !reviewedBlocked && (
            <p className="text-xs text-amber-600">
              Below the {formatDuration(TRAIN_TARGET_SECS)} recommended minimum — the model may
              be low quality.
            </p>
          )}

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => {
                setConfirming(false)
                setError(null)
              }}
              className="px-3 py-1.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void handleTrain()}
              disabled={startDisabled}
              title={
                mode === 'approved' && reviewedBlocked
                  ? `${mmss(approvedDuration)} of ${mmss(TRAIN_MIN_SECS)} minimum approved audio`
                  : undefined
              }
              className="px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting ? 'Starting…' : 'Start training'}
            </button>
          </div>

          {mode === 'approved' && reviewedBlocked && (
            <p className="text-xs text-gray-500">
              {mmss(approvedDuration)} of {mmss(TRAIN_MIN_SECS)} minimum approved audio.
            </p>
          )}

          {error && <p className="text-xs text-red-600">{error}</p>}
        </div>
      )}
    </div>
  )
}
