import { describe, it, expect } from 'vitest'
import { retryPlan, retryGuidance } from './retry'
import type { FailedJob } from '../types/api'

function job(type: string, sourceId: string | null = 'src-1'): FailedJob {
  return { id: 'job-1', type, source_id: sourceId, error: 'boom', completed_at: null }
}

describe('retryPlan', () => {
  it('routes transcription_bulk to a full transcription run', () => {
    expect(retryPlan(job('transcription_bulk'))).toEqual({ kind: 'transcription' })
  })

  it('routes export to a re-export', () => {
    expect(retryPlan(job('export'))).toEqual({ kind: 'export' })
  })

  it('routes scout_speakers to a scout re-run against the same source, not reprocess', () => {
    expect(retryPlan(job('scout_speakers'))).toEqual({ kind: 'scout', sourceId: 'src-1' })
  })

  it('routes vocal_separation and diarisation to reprocess with the right steps', () => {
    expect(retryPlan(job('vocal_separation'))).toEqual({
      kind: 'reprocess',
      sourceId: 'src-1',
      steps: ['separation'],
    })
    expect(retryPlan(job('diarisation'))).toEqual({
      kind: 'reprocess',
      sourceId: 'src-1',
      steps: ['diarisation'],
    })
  })

  it('returns null for extract_audio — extraction failure is terminal', () => {
    expect(retryPlan(job('extract_audio'))).toBeNull()
  })

  it('returns null for transcription_segment — no segment id in the failed-job row', () => {
    expect(retryPlan(job('transcription_segment'))).toBeNull()
  })

  it('returns null for source-scoped types when source_id is missing', () => {
    expect(retryPlan(job('scout_speakers', null))).toBeNull()
    expect(retryPlan(job('vocal_separation', null))).toBeNull()
    expect(retryPlan(job('diarisation', null))).toBeNull()
  })

  it('returns null for unknown job types, including the never-emitted "transcription" type', () => {
    expect(retryPlan(job('mystery_job'))).toBeNull()
    expect(retryPlan(job('transcription'))).toBeNull()
  })
})

describe('retryGuidance', () => {
  it('tells the user to delete and re-upload after a failed extraction', () => {
    expect(retryGuidance('extract_audio')).toMatch(/remove this video and re-upload/i)
  })

  it('has no guidance for retryable types', () => {
    expect(retryGuidance('vocal_separation')).toBeNull()
    expect(retryGuidance('transcription_bulk')).toBeNull()
  })
})
