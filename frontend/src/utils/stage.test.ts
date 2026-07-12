import { describe, it, expect } from 'vitest'
import { deriveStage, stageStates } from './stage'
import type { ProjectDetail, SourceStatus, JobSummary } from '../types/api'

function makeProject(overrides: {
  sourceStatuses?: SourceStatus[]
  activeJobs?: Partial<JobSummary>[]
  referencePath?: string | null
  pendingCount?: number
  maybeCount?: number
  approvedCount?: number
  autoApprovedCount?: number
  belowThresholdCount?: number
  status?: ProjectDetail['status']
}): ProjectDetail {
  const {
    sourceStatuses = [],
    activeJobs = [],
    referencePath = null,
    pendingCount = 0,
    maybeCount = 0,
    approvedCount = 0,
    autoApprovedCount = 0,
    belowThresholdCount = 0,
    status = 'ready',
  } = overrides
  return {
    id: 'p1',
    name: 'Test',
    status,
    created_at: '',
    updated_at: '',
    reference_path: referencePath,
    reference_origin: null,
    config: {
      whisper_model: 'large-v2',
      language: null,
      match_threshold: 0.75,
      target_duration_secs: 1800,
      auto_approve_enabled: false,
      auto_approve_match_threshold: 0.9,
      auto_approve_transcript_threshold: 0.9,
    },
    active_jobs: activeJobs.map((j, i) => ({
      id: j.id ?? `j${i}`,
      type: j.type ?? 'vocal_separation',
      status: j.status ?? 'running',
      progress: j.progress ?? null,
    })),
    recent_failed_jobs: [],
    stats: {
      approved_count: approvedCount,
      approved_duration_secs: 0,
      pending_count: pendingCount,
      maybe_count: maybeCount,
      total_segments: pendingCount + maybeCount + belowThresholdCount + approvedCount + autoApprovedCount,
      auto_approved_count: autoApprovedCount,
      rejected_count: 0,
      below_threshold_count: belowThresholdCount,
      source_coverage: sourceStatuses.map((s, i) => ({
        source_id: `s${i}`,
        filename: `file${i}.mp4`,
        status: s,
        coverage_ratio: 0,
        low_coverage_warning: false,
        error: null,
      })),
    },
  }
}

describe('deriveStage', () => {
  it('returns upload when there are no sources', () => {
    expect(deriveStage(makeProject({}))).toBe('upload')
  })

  // --- Speaker stage: everything before a reference is committed ---

  it('stays on speaker while separation runs for the scan (no reference yet)', () => {
    const p = makeProject({
      sourceStatuses: ['separation_running'],
      activeJobs: [{ type: 'vocal_separation' }],
    })
    expect(deriveStage(p)).toBe('speaker')
  })

  it('returns speaker for a freshly-uploaded source (the whose-voice prompt)', () => {
    const p = makeProject({ sourceStatuses: ['separation_pending'] })
    expect(deriveStage(p)).toBe('speaker')
  })

  it('returns speaker at the reference gate with no reference', () => {
    const p = makeProject({ sourceStatuses: ['diarisation_pending'] })
    expect(deriveStage(p)).toBe('speaker')
  })

  it('stays on speaker while a scout job runs', () => {
    const p = makeProject({
      sourceStatuses: ['diarisation_pending'],
      activeJobs: [{ type: 'scout_speakers' }],
    })
    expect(deriveStage(p)).toBe('speaker')
  })

  it('stays on speaker when separation fails before a reference is set', () => {
    const p = makeProject({ sourceStatuses: ['separation_failed'] })
    expect(deriveStage(p)).toBe('speaker')
  })

  // --- Process stage: reference committed, pipeline proper ---

  it('returns process while a pipeline job runs with a reference set', () => {
    const p = makeProject({
      sourceStatuses: ['separation_running'],
      activeJobs: [{ type: 'vocal_separation' }],
      referencePath: '/data/ref.wav',
    })
    expect(deriveStage(p)).toBe('process')
  })

  it('returns process for an uploaded-clip source queued to start', () => {
    const p = makeProject({ sourceStatuses: ['separation_pending'], referencePath: '/data/ref.wav' })
    expect(deriveStage(p)).toBe('process')
  })

  it('returns process at the gate once a reference is set', () => {
    const p = makeProject({
      sourceStatuses: ['diarisation_pending'],
      referencePath: '/data/ref.wav',
    })
    expect(deriveStage(p)).toBe('process')
  })

  it('returns process when a source failed after a reference is set', () => {
    const p = makeProject({ sourceStatuses: ['separation_failed'], referencePath: '/data/ref.wav' })
    expect(deriveStage(p)).toBe('process')
  })

  it('returns review when sources are complete and segments await review', () => {
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      pendingCount: 5,
      maybeCount: 1,
    })
    expect(deriveStage(p)).toBe('review')
  })

  it('returns export when everything is reviewed', () => {
    const p = makeProject({ sourceStatuses: ['complete'], referencePath: '/data/ref.wav' })
    expect(deriveStage(p)).toBe('export')
  })

  it('returns review (not export) when every segment is below the threshold', () => {
    // Nothing pending/maybe, nothing approved, but segments exist below the
    // match threshold — the user needs to lower it, not export an empty dataset.
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      belowThresholdCount: 12,
    })
    expect(deriveStage(p)).toBe('review')
  })

  it('returns export when there is approved content despite leftover below-threshold', () => {
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      approvedCount: 4,
      belowThresholdCount: 8,
    })
    expect(deriveStage(p)).toBe('export')
  })

  it('returns export while an export job runs', () => {
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      activeJobs: [{ type: 'export' }],
      status: 'exporting',
    })
    expect(deriveStage(p)).toBe('export')
  })

  it('prefers process over review when a new source is added mid-review', () => {
    const p = makeProject({
      sourceStatuses: ['complete', 'uploaded'],
      referencePath: '/data/ref.wav',
      pendingCount: 5,
    })
    expect(deriveStage(p)).toBe('process')
  })
})

describe('stageStates', () => {
  it('marks earlier stages done, current needs_you, later upcoming', () => {
    const p = makeProject({ sourceStatuses: ['diarisation_pending'] })
    const states = stageStates(p)
    expect(states.upload).toBe('done')
    expect(states.speaker).toBe('needs_you')
    expect(states.process).toBe('upcoming')
    expect(states.review).toBe('upcoming')
    expect(states.export).toBe('upcoming')
  })

  it('marks the current stage active while jobs run', () => {
    const p = makeProject({
      sourceStatuses: ['separation_running'],
      activeJobs: [{ type: 'vocal_separation' }],
      referencePath: '/data/ref.wav',
    })
    expect(stageStates(p).process).toBe('active')
  })

  it('marks speaker active while separation runs for the scan', () => {
    const p = makeProject({
      sourceStatuses: ['separation_running'],
      activeJobs: [{ type: 'vocal_separation' }],
    })
    expect(stageStates(p).speaker).toBe('active')
  })
})
