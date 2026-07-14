import { useState, type RefObject } from 'react'
import { Link } from 'react-router-dom'
import type { EngineInfo, Model, ProjectDetail } from '../../types/api'
import {
  deriveStage,
  hasActivePipelineJob,
  stepChip,
  type PipelineStep,
  type StepChip,
} from '../../utils/stage'
import {
  CLEANUP_KNOBS,
  DIARISATION_KNOBS,
  SEPARATION_KNOBS,
  TRANSCRIPTION_KNOBS,
  XTTS_KNOBS,
  type Knob,
} from '../../utils/tuning'
import { formatDurationCoarse } from '../../utils/format'
import { ProgressBar } from '../ui/ProgressBar'
import { StageSettingsPanel } from './StageSettingsPanel'
import { ProjectSettingsPanel } from './ProjectSettingsPanel'
import { VocalsButton } from './VocalsButton'
import { ExportButton } from '../export/ExportButton'
import { TrainPanel } from '../voice/TrainPanel'

interface PipelineStepsProps {
  project: ProjectDetail
  voiceTrainingEnabled: boolean
  /** Healthy-or-not engine list from capabilities — forwarded to the Train
   *  row's TrainPanel to build its engine picker. Omitted (or a single-entry
   *  list) means no picker, an implicit engine. */
  engines?: EngineInfo[]
  /** Settings saved → parent refetches the project. */
  onSaved: () => void
  /** Re-run a step across all eligible sources (steps in reprocess-API terms). */
  onReprocessAll: (steps: string[]) => void
  /** Trigger bulk transcription. */
  onRunTranscription: () => void
  /** Open the cleanup A/B compare modal. */
  onOpenCompare: () => void
  /** The Review row's settings disclosure — the header gear and the
   *  adjust-threshold shortcut expand + scroll to it imperatively. */
  reviewSettingsRef?: RefObject<HTMLDetailsElement>
  /** Trained models (dashboard-owned) — drives the Train row's chip. */
  models?: Model[]
  /** Open + scroll the Models section (the Train row's Models link). */
  onGoToModels?: () => void
  /** A train was enqueued → parent refetches the project AND reloads models. */
  onTrainStarted?: () => void
  /** The Train row — the strip's Train chip and the next-action card scroll to it. */
  trainRowRef?: RefObject<HTMLDivElement>
  /** Header toggle: show advanced-flagged knobs in the settings panels. */
  advanced?: boolean
}

const CHIP_CLASSES: Record<StepChip['tone'], string> = {
  grey: 'bg-gray-100 text-gray-500 dark:bg-gray-700/60 dark:text-gray-400',
  blue: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  amber: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  green: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  red: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
}

// Source statuses from which a step can be re-run (terminal for that source —
// mid-pipeline sources are skipped rather than 409ing one by one).
const RERUNNABLE: Record<'separate' | 'match', string[]> = {
  separate: ['complete', 'separation_failed', 'diarisation_failed'],
  match: ['complete', 'diarisation_failed'],
}

// Sources whose vocals stem exists on disk (separation has completed).
const VOCALS_READY_STATUSES = new Set([
  'diarisation_pending',
  'diarisation_running',
  'diarisation_failed',
  'complete',
])

function StepRow({
  index,
  title,
  chip,
  actions,
  children,
}: {
  index: number
  title: string
  chip: StepChip | { label: string; tone: StepChip['tone'] }
  actions?: React.ReactNode
  children?: React.ReactNode
}) {
  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-3 space-y-2">
      <div className="flex items-center gap-3 flex-wrap mb-3">
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-gray-100 dark:bg-gray-700 text-xs font-semibold text-gray-500 dark:text-gray-400">
          {index}
        </span>
        <span className="text-sm font-medium text-gray-800 dark:text-gray-200">{title}</span>
        <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${CHIP_CLASSES[chip.tone]}`}>
          {chip.label}
        </span>
        <span className="flex-1" />
        {actions}
      </div>
      {children}
    </div>
  )
}

function RunButton({
  label,
  onClick,
  disabled,
  title,
}: {
  label: string
  onClick: () => void
  disabled?: boolean
  title?: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="px-2.5 py-1 text-xs font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-50 disabled:cursor-not-allowed"
    >
      {label}
    </button>
  )
}

// The pipeline as four distinct rows: state, that stage's tuning knobs, and a
// run/re-run affordance. Settings changes apply on the step's next run — the
// Re-run button sits right there so the config → rerun link is explicit.
// Compact count pills for the Review row — the same information as the old
// stats grid, one line tall.
const COUNT_CHIP_CLASSES = 'px-2 py-0.5 rounded-full text-xs font-medium'

function CountChip({ value, label, tone }: { value: number; label: string; tone: string }) {
  return (
    <span className={`${COUNT_CHIP_CLASSES} ${tone} ${value === 0 ? 'opacity-40' : ''}`}>
      {value} {label}
    </span>
  )
}

export function PipelineSteps({
  project,
  voiceTrainingEnabled,
  engines,
  onSaved,
  onReprocessAll,
  onRunTranscription,
  onOpenCompare,
  reviewSettingsRef,
  models,
  onGoToModels,
  onTrainStarted,
  trainRowRef,
  advanced = false,
}: PipelineStepsProps) {
  const busy = hasActivePipelineJob(project)
  const sources = project.stats.source_coverage
  const [transcribing, setTranscribing] = useState(false)

  // Deployment-level XTTS availability (not "is xtts the selected engine") —
  // gates the legacy persisted xtts_* settings disclosure below, which has no
  // effect on a GPT-SoVITS run. `engines` omitted (older callers/tests) means
  // an XTTS-only deployment, matching TrainPanel's own implicit-engine default.
  const xttsAvailable = engines ? engines.some((e) => e.id === 'xtts' && e.healthy) : true

  function chip(step: PipelineStep): StepChip {
    return stepChip(project, step, voiceTrainingEnabled)
  }

  function rerunnable(step: 'separate' | 'match'): boolean {
    if (busy) return false
    const eligible = RERUNNABLE[step]
    return sources.some((s) => eligible.includes(s.status))
  }

  function ranAlready(step: PipelineStep): boolean {
    const c = chip(step)
    return c.label === 'Done' || c.label === 'Failed'
  }

  const separateChip = chip('separate')
  const matchChip = chip('match')
  const transcribeChip = chip('transcribe')
  const transcriptionActive = project.active_jobs.some(
    (j) => j.type === 'transcription_bulk' || j.type === 'transcription_segment',
  )
  const hasSegments = project.stats.total_segments > 0
  const vocalsReady = sources.filter((s) => VOCALS_READY_STATUSES.has(s.status))

  // Review sits between transcription and cleanup in the pipeline — it's the
  // human step, so its chip counts work owed rather than tracking a job.
  const toReview = project.stats.pending_count + project.stats.maybe_count
  const reviewed = project.stats.approved_count + project.stats.auto_approved_count
  const reviewChip: StepChip =
    toReview > 0
      ? { label: `${toReview} to review`, tone: 'blue' }
      : reviewed > 0
        ? { label: 'Done', tone: 'green' }
        : { label: 'Not run yet', tone: 'grey' }

  // Train row (deployments with a healthy voice engine only): job-aware
  // first, then model state.
  const trainActive = project.active_jobs.some(
    (j) => j.type === 'finetune' || j.type === 'dataset_build',
  )
  const trainChip: StepChip = trainActive
    ? { label: 'Running', tone: 'blue' }
    : (models ?? []).some((m) => m.status === 'ready')
      ? { label: 'Done', tone: 'green' }
      : deriveStage(project, voiceTrainingEnabled) === 'train'
        ? { label: 'Ready', tone: 'amber' }
        : { label: 'Not run yet', tone: 'grey' }

  /** A step's settings disclosure. Steps that don't track "already ran"
   *  (cleanup applies at export; train defaults apply at the next train)
   *  omit the step and get the plain saved message. */
  function settingsFor(knobs: Knob[], step?: PipelineStep) {
    return (
      <div className="pt-2">
        <StageSettingsPanel
          projectId={project.id}
          config={project.config}
          knobs={knobs}
          ranAlready={step !== undefined && ranAlready(step)}
          onSaved={onSaved}
          advanced={advanced}
        />
      </div>
    )
  }

  function handleTranscribe() {
    setTranscribing(true)
    try {
      onRunTranscription()
    } finally {
      // The parent refetch flips the chip to Running; this only guards the gap.
      setTranscribing(false)
    }
  }

  return (
    <div className="space-y-2">
      <StepRow
        index={1}
        title="Separate vocals"
        chip={separateChip}
        actions={
          <RunButton
            label="Re-run"
            onClick={() => onReprocessAll(['separation', 'diarisation'])}
            disabled={!rerunnable('separate')}
            title="Re-run vocal separation (and re-match) for all processed sources"
          />
        }
      >
        {vocalsReady.length > 0 && (
          <div className="space-y-1">
            {vocalsReady.map((s) => (
              <VocalsButton
                key={s.source_id}
                projectId={project.id}
                sourceId={s.source_id}
                filename={s.filename}
              />
            ))}
          </div>
        )}
        {settingsFor(SEPARATION_KNOBS, 'separate')}
      </StepRow>

      <StepRow
        index={2}
        title="Match speaker"
        chip={matchChip}
        actions={
          <RunButton
            label="Re-run"
            onClick={() => onReprocessAll(['diarisation'])}
            disabled={!rerunnable('match')}
            title="Re-run speaker matching for all processed sources"
          />
        }
      >
        {settingsFor(DIARISATION_KNOBS, 'match')}
      </StepRow>

      <StepRow
        index={3}
        title="Transcribe"
        chip={transcribeChip}
        actions={
          <RunButton
            label={transcribeChip.label === 'Done' ? 'Re-run' : 'Run'}
            onClick={handleTranscribe}
            disabled={busy || transcribing || transcriptionActive || !hasSegments}
            title="Transcribe all untranscribed segments"
          />
        }
      >
        {settingsFor(TRANSCRIPTION_KNOBS, 'transcribe')}
      </StepRow>

      <StepRow
        index={4}
        title="Review"
        chip={reviewChip}
        actions={
          hasSegments ? (
            <Link
              to={`/projects/${project.id}/review`}
              className="px-2.5 py-1 text-xs font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50"
            >
              Open review →
            </Link>
          ) : undefined
        }
      >
        {hasSegments && (
          <>
            <div className="flex flex-wrap items-center gap-1.5 pt-1 pb-3">
              <CountChip value={project.stats.approved_count} label="approved" tone="bg-green-50 text-green-800 dark:bg-green-900/30 dark:text-green-300" />
              <CountChip value={project.stats.auto_approved_count} label="auto-approved" tone="bg-teal-50 text-teal-800 dark:bg-teal-900/30 dark:text-teal-300" />
              <CountChip value={project.stats.pending_count} label="pending" tone="bg-gray-100 text-gray-700 dark:bg-gray-700/60 dark:text-gray-300" />
              <CountChip value={project.stats.maybe_count} label="maybe" tone="bg-yellow-50 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300" />
              <CountChip value={project.stats.rejected_count} label="rejected" tone="bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-300" />
              <CountChip value={project.stats.below_threshold_count} label="below threshold" tone="bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400" />
            </div>
            <div>
              <div className="flex items-baseline justify-between mb-1">
                <span className="text-xs font-medium text-gray-600 dark:text-gray-400">
                  Approved duration
                  <span className="ml-1 font-normal text-gray-400 dark:text-gray-500">(includes auto-approved)</span>
                </span>
              </div>
              <ProgressBar
                value={
                  project.config.target_duration_secs > 0
                    ? (project.stats.approved_duration_secs / project.config.target_duration_secs) * 100
                    : 0
                }
                label={`${formatDurationCoarse(project.stats.approved_duration_secs)} / ${formatDurationCoarse(project.config.target_duration_secs)}`}
                color="green"
              />
            </div>
          </>
        )}
        <details ref={reviewSettingsRef} className="group scroll-mt-4 pt-2">
          <summary className="cursor-pointer select-none text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition-colors list-none flex items-center gap-1">
            <span className="inline-block transition-transform group-open:rotate-90">▸</span>
            Settings
          </summary>
          <div className="mt-3">
            <ProjectSettingsPanel projectId={project.id} config={project.config} onSaved={onSaved} />
          </div>
        </details>
      </StepRow>

      <StepRow
        index={5}
        title="Clean & package"
        chip={{ label: 'Runs during export', tone: 'grey' }}
        actions={
          <RunButton
            label="Compare…"
            onClick={onOpenCompare}
            disabled={!hasSegments}
            title="A/B test cleanup settings on a segment"
          />
        }
      >
        <ExportButton project={project} onStarted={onSaved} size="sm" />
        {settingsFor(CLEANUP_KNOBS)}
      </StepRow>

      {voiceTrainingEnabled && (
        <div ref={trainRowRef} className="scroll-mt-4">
          <StepRow
            index={6}
            title="Train"
            chip={trainChip}
            actions={
              <RunButton
                label="Models →"
                onClick={() => onGoToModels?.()}
                title="Trained models and voice preview"
              />
            }
          >
            <TrainPanel
              project={project}
              models={models ?? []}
              engines={engines}
              onStarted={() => onTrainStarted?.()}
            />
            {/* Persisted fine-tune settings (xtts_* config) — the single source
                of truth applied to every XTTS training run. Hidden when XTTS
                isn't part of this deployment: it has no effect on a
                GPT-SoVITS run and would sit confusingly next to that
                engine's own Advanced fieldset (a second, differently-scoped
                "Batch size" field). Gated on XTTS *availability*, not the
                picker's current selection — the persisted settings still
                drive xtts runs even when both engines are healthy. */}
            {xttsAvailable && settingsFor(XTTS_KNOBS)}
          </StepRow>
        </div>
      )}
    </div>
  )
}
