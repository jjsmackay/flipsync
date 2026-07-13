import type { ProjectDetail } from '../types/api'

// The dashboard is organised around five user-facing stages. deriveStage maps
// the polled project state to the single stage the user should care about now.
// The terminal stage depends on the deployment: Export by default, or Train when
// the XTTS voice service is present (export stays reachable as a button).

export type Stage = 'upload' | 'speaker' | 'process' | 'review' | 'export' | 'train'

const BASE_STAGES: Stage[] = ['upload', 'speaker', 'process', 'review', 'export']
const XTTS_STAGES: Stage[] = ['upload', 'speaker', 'process', 'review', 'train']

/** The ordered stage strip for this deployment. */
export function stagesFor(xttsEnabled: boolean): Stage[] {
  return xttsEnabled ? XTTS_STAGES : BASE_STAGES
}

export const STAGE_LABELS: Record<Stage, string> = {
  upload: 'Upload',
  speaker: 'Speaker',
  process: 'Process',
  review: 'Review',
  export: 'Export',
  train: 'Train',
}

// Source statuses that mean pipeline work is still owed (queued, running, or
// failed and awaiting a retry) once a reference exists. diarisation_pending is
// included here: with a reference set it means "ready to continue".
const PROCESSING_SOURCE_STATUSES = new Set([
  'uploaded',
  'extracting',
  'separation_pending',
  'separation_running',
  'diarisation_pending',
  'diarisation_running',
  'extraction_failed',
  'separation_failed',
  'diarisation_failed',
])

// Job types that don't belong to the Process stage even while active: scout jobs
// belong to Speaker, export jobs to Export, and the voice (dataset/train/preview)
// jobs run independently of — and can far outlast — the extract/separate/diarise/
// transcribe/cleanup pipeline, so they must not pull the stage strip back to
// Process while the project sits in Review or Export.
const NON_PIPELINE_JOB_TYPES = new Set([
  'export',
  'scout_speakers',
  'dataset_build',
  'finetune',
  'preview',
])

/** Active jobs that belong to the extract/separate/diarise/transcribe/cleanup
 *  pipeline proper — excludes export, scout, and voice (dataset/train/preview)
 *  jobs, which must not drive the Process stage or its "busy" chip. */
export function pipelineJobs(jobs: ProjectDetail['active_jobs']): ProjectDetail['active_jobs'] {
  return jobs.filter((j) => !NON_PIPELINE_JOB_TYPES.has(j.type))
}

export function deriveStage(project: ProjectDetail, xttsEnabled = false): Stage {
  const sources = project.stats.source_coverage
  if (sources.length === 0) return 'upload'

  // reference_path is the Speaker/Process divider. The "whose voice?" prompt is
  // the only trigger for separation, and the upload-a-clip path sets a
  // reference before separation runs — so a project with sources but no
  // reference is always still in the Speaker stage: the prompt, the separation
  // run that feeds the scan, the scout scan itself, or picking a candidate.
  // Everything downstream of a committed reference is the pipeline proper.
  if (!project.reference_path) return 'speaker'

  if (pipelineJobs(project.active_jobs).length > 0) return 'process'

  if (sources.some((s) => PROCESSING_SOURCE_STATUSES.has(s.status))) return 'process'

  const { pending_count, maybe_count, approved_count, auto_approved_count, below_threshold_count } =
    project.stats
  if (pending_count + maybe_count > 0) return 'review'

  // Nothing queued for review and nothing approved yet, but segments are sitting
  // below the match threshold: the run matched poorly, not "done". Keep the user
  // in Review — which shows the lower-the-threshold guidance — rather than
  // sending them to Export with a misleading "ready to export".
  if (approved_count + auto_approved_count === 0 && below_threshold_count > 0) return 'review'

  // Terminal stage: Train when the voice service is deployed, else Export.
  return xttsEnabled ? 'train' : 'export'
}

export type StageState = 'done' | 'active' | 'needs_you' | 'upcoming'

/** Chip state for each stage given the current one. */
export function stageStates(
  project: ProjectDetail,
  xttsEnabled = false,
): Record<Stage, StageState> {
  const stages = stagesFor(xttsEnabled)
  const current = deriveStage(project, xttsEnabled)
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
