import type { ProjectDetail } from '../types/api'

// The dashboard is organised around user-facing stages. deriveStage maps the
// polled project state to the single stage the user should care about now.
// The pipeline proper is three distinct steps (separate → match → transcribe),
// each with its own strip chip and step row. The terminal stage depends on the
// deployment: Export by default, or Train when a voice engine (XTTS and/or
// GPT-SoVITS — capabilities.voice_training) is healthy (export stays reachable
// as a button).

export type Stage =
  | 'upload'
  | 'speaker'
  | 'separate'
  | 'match'
  | 'transcribe'
  | 'review'
  | 'export'
  | 'train'

/** The three pipeline steps that exist as rows in the Process section. */
export type PipelineStep = 'separate' | 'match' | 'transcribe'

const BASE_STAGES: Stage[] = ['upload', 'speaker', 'separate', 'match', 'transcribe', 'review', 'export']
const VOICE_STAGES: Stage[] = ['upload', 'speaker', 'separate', 'match', 'transcribe', 'review', 'train']

/** The ordered stage strip for this deployment. */
export function stagesFor(voiceTrainingEnabled: boolean): Stage[] {
  return voiceTrainingEnabled ? VOICE_STAGES : BASE_STAGES
}

export const STAGE_LABELS: Record<Stage, string> = {
  upload: 'Upload',
  speaker: 'Speaker',
  separate: 'Separate',
  match: 'Match',
  transcribe: 'Transcribe',
  review: 'Review',
  export: 'Export',
  train: 'Train',
}

// Source statuses owned by each pipeline step. A failed status keeps the step
// current (work is still owed there — queued, running, or awaiting a retry).
const SEPARATE_SOURCE_STATUSES = new Set([
  'uploaded',
  'extracting',
  'separation_pending',
  'separation_running',
  'extraction_failed',
  'separation_failed',
])
const MATCH_SOURCE_STATUSES = new Set([
  'diarisation_pending',
  'diarisation_running',
  'diarisation_failed',
])

// Job types owned by each pipeline step.
const SEPARATE_JOB_TYPES = new Set(['extract_audio', 'vocal_separation'])
const MATCH_JOB_TYPES = new Set(['diarisation'])
const TRANSCRIBE_JOB_TYPES = new Set(['transcription_bulk', 'transcription_segment'])

// Job types that don't belong to any pipeline step even while active: scout
// jobs belong to Speaker, export jobs to Export, and the voice (dataset/train/
// preview) jobs plus ephemeral tuning previews run independently of — and can
// far outlast — the pipeline, so they must not pull the stage strip back while
// the project sits in Review or Export.
const NON_PIPELINE_JOB_TYPES = new Set([
  'export',
  'scout_speakers',
  'dataset_build',
  'finetune',
  'preview',
  'tuning_preview',
])

/** Active jobs that belong to the extract/separate/diarise/transcribe/cleanup
 *  pipeline proper — excludes export, scout, voice (dataset/train/preview) and
 *  tuning-preview jobs, which must not drive the stage or a "busy" chip. */
export function pipelineJobs(jobs: ProjectDetail['active_jobs']): ProjectDetail['active_jobs'] {
  return jobs.filter((j) => !NON_PIPELINE_JOB_TYPES.has(j.type))
}

/** True while any job that belongs to the pipeline proper is queued/running. */
export function hasActivePipelineJob(project: ProjectDetail): boolean {
  return pipelineJobs(project.active_jobs).length > 0
}

export function deriveStage(project: ProjectDetail, voiceTrainingEnabled = false): Stage {
  const sources = project.stats.source_coverage
  if (sources.length === 0) return 'upload'

  // reference_path is the Speaker/pipeline divider. The "whose voice?" prompt
  // is the only trigger for separation, and the upload-a-clip path sets a
  // reference before separation runs — so a project with sources but no
  // reference is always still in the Speaker stage: the prompt, the separation
  // run that feeds the scan, the scout scan itself, or picking a candidate.
  // Everything downstream of a committed reference is the pipeline proper.
  if (!project.reference_path) return 'speaker'

  const activePipeline = pipelineJobs(project.active_jobs)

  if (
    activePipeline.some((j) => SEPARATE_JOB_TYPES.has(j.type)) ||
    sources.some((s) => SEPARATE_SOURCE_STATUSES.has(s.status))
  ) {
    return 'separate'
  }

  if (
    activePipeline.some((j) => MATCH_JOB_TYPES.has(j.type)) ||
    sources.some((s) => MATCH_SOURCE_STATUSES.has(s.status))
  ) {
    return 'match'
  }

  // Transcription is auto-chained after matching (and re-runnable from Review),
  // so this step is current only while a transcription job actually runs; a
  // failed transcription surfaces via the failed-jobs panel while the project
  // sits in Review with its untranscribed segments.
  if (activePipeline.some((j) => TRANSCRIBE_JOB_TYPES.has(j.type))) {
    return 'transcribe'
  }

  const { pending_count, maybe_count, approved_count, auto_approved_count, below_threshold_count } =
    project.stats
  if (pending_count + maybe_count > 0) return 'review'

  // Nothing queued for review and nothing approved yet, but segments are sitting
  // below the match threshold: the run matched poorly, not "done". Keep the user
  // in Review — which shows the lower-the-threshold guidance — rather than
  // sending them to Export with a misleading "ready to export".
  if (approved_count + auto_approved_count === 0 && below_threshold_count > 0) return 'review'

  // Terminal stage: Train when a voice engine is healthy, else Export.
  return voiceTrainingEnabled ? 'train' : 'export'
}

export type StageState = 'done' | 'active' | 'needs_you' | 'upcoming'

/** Chip state for each stage given the current one. */
export function stageStates(
  project: ProjectDetail,
  voiceTrainingEnabled = false,
): Record<Stage, StageState> {
  const stages = stagesFor(voiceTrainingEnabled)
  const current = deriveStage(project, voiceTrainingEnabled)
  const currentIdx = stages.indexOf(current)
  const busy = pipelineJobs(project.active_jobs).length > 0
  const states = {} as Record<Stage, StageState>
  for (const [idx, stage] of stages.entries()) {
    if (idx < currentIdx) states[stage] = 'done'
    else if (idx > currentIdx) states[stage] = 'upcoming'
    else states[stage] = busy ? 'active' : 'needs_you'
  }
  return states
}

export interface StepChip {
  label: string
  tone: 'grey' | 'blue' | 'amber' | 'green' | 'red'
}

// Source statuses that mean a step failed (chip override — the stage machinery
// keeps the step "current", but the row should read as failed, not ready).
const STEP_FAILED_STATUSES: Record<PipelineStep, Set<string>> = {
  separate: new Set(['extraction_failed', 'separation_failed']),
  match: new Set(['diarisation_failed']),
  // Transcription failures don't mark sources; they surface via the
  // failed-jobs panel, so the transcribe row never shows a red chip.
  transcribe: new Set(),
}

/** Status chip for a pipeline step row in the Process section. */
export function stepChip(
  project: ProjectDetail,
  step: PipelineStep,
  voiceTrainingEnabled = false,
): StepChip {
  const failed = STEP_FAILED_STATUSES[step]
  if (project.stats.source_coverage.some((s) => failed.has(s.status))) {
    return { label: 'Failed', tone: 'red' }
  }
  switch (stageStates(project, voiceTrainingEnabled)[step]) {
    case 'done':
      return { label: 'Done', tone: 'green' }
    case 'active':
      return { label: 'Running', tone: 'blue' }
    case 'needs_you':
      return { label: 'Ready', tone: 'amber' }
    default:
      return { label: 'Not run yet', tone: 'grey' }
  }
}
