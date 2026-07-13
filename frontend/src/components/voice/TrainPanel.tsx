import { useState } from 'react'
import type { ProjectDetail, Model, CreateModelRequest, ModelParams, JobSummary } from '../../types/api'
import { createModel, ApiError } from '../../api/client'
import { formatDuration } from '../../utils/format'
import { errorMessage } from '../../utils/errors'
import { jobLabel } from '../../utils/labels'
import { ProgressBar } from '../ui/ProgressBar'
import { changedValues, configValues, XTTS_KNOBS, type TuningKey, type TuningValue, type TuningValues } from '../../utils/tuning'
import { KnobFields } from '../project/KnobFields'

// Maps the XTTS tuning keys (shared with the project config + create-project
// form) to the CreateModelRequest.params field names — the two naming schemes
// diverge only by the `xtts_` prefix.
const XTTS_PARAM_KEYS: Partial<Record<TuningKey, keyof ModelParams>> = {
  xtts_epochs: 'epochs',
  xtts_batch_size: 'batch_size',
  xtts_grad_accum: 'grad_accum',
  xtts_learning_rate: 'learning_rate',
}

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

const ERROR_MESSAGES: Record<string, string> = {
  insufficient_dataset: 'Not enough usable audio to train (300 s minimum after filtering).',
  finetune_in_progress: 'A model for this project is already training. Wait for it to finish.',
  xtts_unavailable: 'The voice service is not deployed or is unhealthy.',
}

function TrainingProgressCard({ job }: { job: JobSummary }) {
  const detail = job.type === 'finetune' ? job.progress_detail : null

  return (
    <div className="rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/20 p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-semibold text-blue-800 dark:text-blue-300">
          {jobLabel(job.type)}
        </span>
        <span className="text-xs text-blue-500 dark:text-blue-400 capitalize">{detail?.phase ?? job.status}</span>
      </div>
      <ProgressBar value={job.progress ?? 0} color="blue" />
      {detail && (
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-blue-800 dark:text-blue-300 sm:grid-cols-4">
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
  const advancedBaseline = configValues(project.config, XTTS_KNOBS)
  const [advancedValues, setAdvancedValues] = useState<TuningValues>(advancedBaseline)

  function handleAdvancedChange(key: TuningKey, value: TuningValue) {
    setAdvancedValues((prev) => ({ ...prev, [key]: value }))
  }

  const approvedDuration = project.stats.approved_duration_secs

  const activeJob =
    project.active_jobs.find((j) => j.type === 'finetune') ??
    project.active_jobs.find((j) => j.type === 'dataset_build')
  const hasInProgressModel = models.some((m) => m.status === 'pending' || m.status === 'training')
  const busy = Boolean(activeJob) || hasInProgressModel

  const reviewedBlocked = approvedDuration < TRAIN_MIN_SECS
  const belowTarget = approvedDuration < TRAIN_TARGET_SECS
  const startDisabled = submitting || (mode === 'approved' && reviewedBlocked)
  const minimumReason = `${formatDuration(approvedDuration)} of ${formatDuration(TRAIN_MIN_SECS)} minimum approved audio.`

  async function handleTrain() {
    setError(null)
    setSubmitting(true)
    const body: CreateModelRequest =
      mode === 'auto'
        ? { dataset: { mode: 'auto', min_confidence: minConfidence } }
        : { dataset: { mode: 'approved' } }
    const changedAdvanced = changedValues(XTTS_KNOBS, advancedValues, advancedBaseline)
    if (Object.keys(changedAdvanced).length > 0) {
      const params: Partial<ModelParams> = {}
      for (const [key, value] of Object.entries(changedAdvanced)) {
        const paramKey = XTTS_PARAM_KEYS[key as TuningKey]
        if (paramKey) params[paramKey] = value as number
      }
      body.params = params
    }
    try {
      await createModel(project.id, body)
      setConfirming(false)
      onStarted()
    } catch (err) {
      if (err instanceof ApiError) {
        setError(ERROR_MESSAGES[err.error] ?? err.message)
      } else {
        setError(errorMessage(err, 'Failed to start training.'))
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
      <div className="rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/20 p-4 text-sm text-blue-800 dark:text-blue-300">
        Training queued…
      </div>
    )
  }

  // Rendered inside the pipeline's Train step row — no card wrapper of its own.
  // Approved-duration progress lives on the Review row; only the training-
  // specific thresholds (300 s minimum, 30 min recommended) are surfaced here.
  return (
    <div className="space-y-3">
      {!confirming ? (
        <>
          <button
            type="button"
            onClick={() => {
              // Reseed from config on every open: defaults saved via the Train
              // settings disclosure since mount must not read as stale per-run
              // overrides (changedValues compares against the CURRENT config).
              setAdvancedValues(configValues(project.config, XTTS_KNOBS))
              setConfirming(true)
            }}
            disabled={reviewedBlocked}
            className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Train voice model
          </button>
          {reviewedBlocked && (
            <p className="text-xs text-gray-500 dark:text-gray-400">{minimumReason}</p>
          )}
        </>
      ) : (
        <div className="space-y-3">
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Training data</legend>
            <label className="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-300">
              <input
                type="radio"
                name="train-mode"
                checked={mode === 'approved'}
                onChange={() => setMode('approved')}
                className="mt-0.5"
              />
              <span>
                <span className="font-medium">Reviewed</span>
                <span className="block text-xs text-gray-500 dark:text-gray-400">
                  Only segments you have approved.
                </span>
              </span>
            </label>
            <label className="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-300">
              <input
                type="radio"
                name="train-mode"
                checked={mode === 'auto'}
                onChange={() => setMode('auto')}
                className="mt-0.5"
              />
              <span>
                <span className="font-medium">Train without review</span>
                <span className="block text-xs text-amber-600 dark:text-amber-400">
                  Trades quality for speed — uses unreviewed high-confidence segments.
                  Wrong transcripts degrade the model.
                </span>
              </span>
            </label>
          </fieldset>

          <details className="group">
            <summary className="cursor-pointer select-none text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition-colors list-none flex items-center gap-1">
              <span className="inline-block transition-transform group-open:rotate-90">▸</span>
              Advanced
            </summary>
            <div className="mt-3">
              <KnobFields
                knobs={XTTS_KNOBS}
                values={advancedValues}
                onChange={handleAdvancedChange}
                idPrefix="train-adv"
              />
            </div>
          </details>

          {mode === 'auto' && (
            <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
              <span className="w-32">Confidence floor</span>
              <input
                type="number"
                min={0}
                max={1}
                step={0.01}
                value={minConfidence}
                onChange={(e) => setMinConfidence(Number(e.target.value))}
                className="w-24 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-100 px-2 py-1 text-sm"
              />
            </label>
          )}

          {mode === 'approved' && belowTarget && !reviewedBlocked && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
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
              className="px-3 py-1.5 text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void handleTrain()}
              disabled={startDisabled}
              title={mode === 'approved' && reviewedBlocked ? minimumReason : undefined}
              className="px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting ? 'Starting…' : 'Start training'}
            </button>
          </div>

          {mode === 'approved' && reviewedBlocked && (
            <p className="text-xs text-gray-500 dark:text-gray-400">{minimumReason}</p>
          )}

          {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
        </div>
      )}
    </div>
  )
}
