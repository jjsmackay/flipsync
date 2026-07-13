import type { FailedJob } from '../types/api'

// How (and whether) a failed job can be retried from the dashboard. The failed-job
// rows from GET /projects/{id} carry only {id, type, source_id, error, completed_at} —
// no params — so job types whose retry needs more than a source id (e.g.
// transcription_segment, which needs a segment id) are not retryable from here.

export type RetryPlan =
  | { kind: 'transcription' }
  | { kind: 'export' }
  | { kind: 'scout'; sourceId: string }
  // Reprocess retries go through the normal confirm flow: submit WITHOUT
  // confirm, surface the would_invalidate_approvals dialog on 409.
  | { kind: 'reprocess'; sourceId: string; steps: string[] }

export function retryPlan(job: FailedJob): RetryPlan | null {
  switch (job.type) {
    case 'transcription_bulk':
      return { kind: 'transcription' }
    case 'export':
      return { kind: 'export' }
    case 'scout_speakers':
      return job.source_id ? { kind: 'scout', sourceId: job.source_id } : null
    case 'vocal_separation':
      return job.source_id
        ? { kind: 'reprocess', sourceId: job.source_id, steps: ['separation'] }
        : null
    case 'diarisation':
      return job.source_id
        ? { kind: 'reprocess', sourceId: job.source_id, steps: ['diarisation'] }
        : null
    // extract_audio: extraction_failed is terminal by design — no retry.
    // transcription_segment: the segment id isn't in the failed-job row.
    default:
      return null
  }
}

/** Guidance shown in place of a Retry button for non-retryable job types. */
export function retryGuidance(jobType: string): string | null {
  if (jobType === 'extract_audio') {
    return 'Extraction failed — remove this video and re-upload it.'
  }
  return null
}
