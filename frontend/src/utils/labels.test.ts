import { describe, it, expect } from 'vitest'
import {
  SOURCE_STATUS_LABELS,
  PROJECT_STATUS_LABELS,
  MODEL_STATUS_LABELS,
  JOB_LABELS,
  jobLabel,
  modelStatusLabel,
  statusLabel,
} from './labels'

const SOURCE_STATUSES = [
  'uploaded',
  'extracting',
  'extraction_failed',
  'separation_pending',
  'separation_running',
  'separation_failed',
  'diarisation_pending',
  'diarisation_running',
  'diarisation_failed',
  'complete',
] as const

const PROJECT_STATUSES = [
  'new',
  'ready',
  'processing',
  'awaiting_reference',
  'review',
  'exporting',
  'exported',
] as const

const MODEL_STATUSES = ['pending', 'training', 'ready', 'failed', 'cancelled'] as const

const JOB_TYPES = [
  'extract_audio',
  'vocal_separation',
  'diarisation',
  'scout_speakers',
  'transcription_bulk',
  'transcription_segment',
  'export',
] as const

describe('labels', () => {
  it('covers every source status', () => {
    for (const s of SOURCE_STATUSES) {
      expect(SOURCE_STATUS_LABELS[s], s).toBeTruthy()
    }
  })

  it('covers every project status', () => {
    for (const s of PROJECT_STATUSES) {
      expect(PROJECT_STATUS_LABELS[s], s).toBeTruthy()
    }
  })

  it('covers every model status', () => {
    for (const s of MODEL_STATUSES) {
      expect(MODEL_STATUS_LABELS[s], s).toBeTruthy()
      expect(modelStatusLabel(s), s).toBe(MODEL_STATUS_LABELS[s])
    }
  })

  it('scopes model statuses away from the generic chain (collision-prone names)', () => {
    // 'pending' and 'ready' mean different things for segments/projects — the
    // model labels must not leak into statusLabel.
    expect(modelStatusLabel('pending')).toBe('Queued')
    expect(statusLabel('pending')).toBe('pending')
    expect(statusLabel('ready')).toBe('Ready')
  })

  it('covers every job type', () => {
    for (const t of JOB_TYPES) {
      expect(JOB_LABELS[t], t).toBeTruthy()
    }
  })

  it('never leaks internal jargon to the user', () => {
    const allLabels = [
      ...Object.values(SOURCE_STATUS_LABELS),
      ...Object.values(PROJECT_STATUS_LABELS),
      ...Object.values(MODEL_STATUS_LABELS),
      ...Object.values(JOB_LABELS),
    ]
    for (const label of allLabels) {
      expect(label).not.toMatch(/step ?\d/i)
      expect(label).not.toMatch(/_/)
      expect(label.toLowerCase()).not.toMatch(/diarisation/)
    }
  })

  it('falls back to plain words for unknown values', () => {
    expect(jobLabel('some_new_job')).toBe('some new job')
    expect(statusLabel('below_threshold')).toBe('below threshold')
  })
})
