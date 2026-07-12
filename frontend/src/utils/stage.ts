import type { ProjectDetail } from '../types/api'

// The dashboard is organised around five user-facing stages. deriveStage maps
// the polled project state to the single stage the user should care about now.

export type Stage = 'upload' | 'speaker' | 'process' | 'review' | 'export'

export const STAGES: Stage[] = ['upload', 'speaker', 'process', 'review', 'export']

export const STAGE_LABELS: Record<Stage, string> = {
  upload: 'Upload',
  speaker: 'Speaker',
  process: 'Process',
  review: 'Review',
  export: 'Export',
}

// Source statuses that mean processing is still owed (queued, running, or
// failed and awaiting a retry). diarisation_pending is handled separately —
// it is the reference gate.
const PROCESSING_SOURCE_STATUSES = new Set([
  'uploaded',
  'extracting',
  'separation_pending',
  'separation_running',
  'diarisation_running',
  'extraction_failed',
  'separation_failed',
  'diarisation_failed',
])

export function deriveStage(project: ProjectDetail): Stage {
  const sources = project.stats.source_coverage
  if (sources.length === 0) return 'upload'

  // Scout jobs belong to the Speaker stage and export jobs to Export — only
  // the pipeline proper counts as Process.
  const processingJobs = project.active_jobs.filter(
    (j) => j.type !== 'export' && j.type !== 'scout_speakers',
  )
  if (processingJobs.length > 0) return 'process'

  const gated = sources.some((s) => s.status === 'diarisation_pending')
  if (gated && !project.reference_path) return 'speaker'
  if (gated || sources.some((s) => PROCESSING_SOURCE_STATUSES.has(s.status))) return 'process'

  if (project.stats.pending_count + project.stats.maybe_count > 0) return 'review'
  return 'export'
}

export type StageState = 'done' | 'active' | 'needs_you' | 'upcoming'

/** Chip state for each stage given the current one. */
export function stageStates(project: ProjectDetail): Record<Stage, StageState> {
  const current = deriveStage(project)
  const currentIdx = STAGES.indexOf(current)
  const busy = project.active_jobs.length > 0
  const states = {} as Record<Stage, StageState>
  for (const [idx, stage] of STAGES.entries()) {
    if (idx < currentIdx) states[stage] = 'done'
    else if (idx > currentIdx) states[stage] = 'upcoming'
    else states[stage] = busy ? 'active' : 'needs_you'
  }
  return states
}
