import type { ProjectStatus, SourceStatus } from '../types/api'

// Single source of user-facing names for internal identifiers. Internal
// statuses and job types stay technical (they match the API and DB); anything
// rendered to the user goes through these maps.

export const SOURCE_STATUS_LABELS: Record<SourceStatus, string> = {
  uploaded: 'Uploaded',
  extracting: 'Extracting audio',
  extraction_failed: 'Extraction failed',
  separation_pending: 'Queued',
  separation_running: 'Separating vocals',
  separation_failed: 'Vocal separation failed',
  diarisation_pending: 'Waiting for speaker',
  diarisation_running: 'Matching speaker',
  diarisation_failed: 'Speaker matching failed',
  complete: 'Processed',
}

export const PROJECT_STATUS_LABELS: Record<ProjectStatus, string> = {
  new: 'New',
  ready: 'Ready',
  processing: 'Processing',
  awaiting_reference: 'Needs speaker',
  review: 'Reviewing',
  exporting: 'Exporting',
  exported: 'Exported',
}

export const JOB_LABELS: Record<string, string> = {
  extract_audio: 'Extracting audio',
  vocal_separation: 'Separating vocals',
  diarisation: 'Matching speaker',
  scout_speakers: 'Scanning for speakers',
  transcription: 'Transcribing',
  transcription_bulk: 'Transcribing segments',
  transcription_segment: 'Transcribing segment',
  cleanup: 'Cleaning up audio',
  export: 'Exporting dataset',
  dataset_build: 'Building dataset',
  finetune: 'Fine-tuning voice',
  preview: 'Synthesising preview',
}

export function jobLabel(type: string): string {
  return JOB_LABELS[type] ?? type.replace(/_/g, ' ')
}

/** Label for any status string; segment statuses fall through to plain words. */
export function statusLabel(status: string): string {
  return (
    SOURCE_STATUS_LABELS[status as SourceStatus] ??
    PROJECT_STATUS_LABELS[status as ProjectStatus] ??
    status.replace(/_/g, ' ')
  )
}
