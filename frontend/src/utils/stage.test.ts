import { describe, it, expect } from 'vitest'
import { deriveStage, stageStates, stagesFor, stepChip } from './stage'
import { TUNING_DEFAULTS } from './tuning'
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
      ...TUNING_DEFAULTS,
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

  // --- Pipeline stages: reference committed, distinct steps ---

  it('returns separate while a separation job runs with a reference set', () => {
    const p = makeProject({
      sourceStatuses: ['separation_running'],
      activeJobs: [{ type: 'vocal_separation' }],
      referencePath: '/data/ref.wav',
    })
    expect(deriveStage(p)).toBe('separate')
  })

  it('returns separate for an uploaded-clip source queued to start', () => {
    const p = makeProject({ sourceStatuses: ['separation_pending'], referencePath: '/data/ref.wav' })
    expect(deriveStage(p)).toBe('separate')
  })

  it('returns separate while audio extraction runs', () => {
    const p = makeProject({
      sourceStatuses: ['extracting'],
      activeJobs: [{ type: 'extract_audio' }],
      referencePath: '/data/ref.wav',
    })
    expect(deriveStage(p)).toBe('separate')
  })

  it('returns separate when separation failed after a reference is set', () => {
    const p = makeProject({ sourceStatuses: ['separation_failed'], referencePath: '/data/ref.wav' })
    expect(deriveStage(p)).toBe('separate')
  })

  it('returns match at the gate once a reference is set', () => {
    const p = makeProject({
      sourceStatuses: ['diarisation_pending'],
      referencePath: '/data/ref.wav',
    })
    expect(deriveStage(p)).toBe('match')
  })

  it('returns match while diarisation runs', () => {
    const p = makeProject({
      sourceStatuses: ['diarisation_running'],
      activeJobs: [{ type: 'diarisation' }],
      referencePath: '/data/ref.wav',
    })
    expect(deriveStage(p)).toBe('match')
  })

  it('returns match when diarisation failed', () => {
    const p = makeProject({ sourceStatuses: ['diarisation_failed'], referencePath: '/data/ref.wav' })
    expect(deriveStage(p)).toBe('match')
  })

  it('returns transcribe while a bulk transcription job runs', () => {
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      activeJobs: [{ type: 'transcription_bulk' }],
      pendingCount: 5,
    })
    expect(deriveStage(p)).toBe('transcribe')
  })

  it('returns transcribe while a single-segment transcription job runs', () => {
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      activeJobs: [{ type: 'transcription_segment' }],
      pendingCount: 5,
    })
    expect(deriveStage(p)).toBe('transcribe')
  })

  it('prefers separate over match when sources straddle both steps', () => {
    const p = makeProject({
      sourceStatuses: ['separation_running', 'diarisation_pending'],
      activeJobs: [{ type: 'vocal_separation' }],
      referencePath: '/data/ref.wav',
    })
    expect(deriveStage(p)).toBe('separate')
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

  // --- Non-pipeline jobs must not regress the stage ---

  it('stays on export (not a pipeline step) while a dataset_build job runs', () => {
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      activeJobs: [{ type: 'dataset_build' }],
    })
    expect(deriveStage(p)).toBe('export')
  })

  it('stays on export (not a pipeline step) while a finetune job runs', () => {
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      activeJobs: [{ type: 'finetune' }],
    })
    expect(deriveStage(p)).toBe('export')
  })

  it('stays on export (not a pipeline step) while a preview job runs', () => {
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      activeJobs: [{ type: 'preview' }],
    })
    expect(deriveStage(p)).toBe('export')
  })

  it('stays on review while a tuning_preview job runs', () => {
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      activeJobs: [{ type: 'tuning_preview' }],
      pendingCount: 3,
    })
    expect(deriveStage(p)).toBe('review')
  })

  it('stays on review (not a pipeline step) while a finetune job runs alongside pending segments', () => {
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      activeJobs: [{ type: 'finetune' }],
      pendingCount: 3,
    })
    expect(deriveStage(p)).toBe('review')
  })

  it('prefers separate over review when a new source is added mid-review', () => {
    const p = makeProject({
      sourceStatuses: ['complete', 'uploaded'],
      referencePath: '/data/ref.wav',
      pendingCount: 5,
    })
    expect(deriveStage(p)).toBe('separate')
  })
})

describe('stageStates', () => {
  it('marks earlier stages done, current needs_you, later upcoming', () => {
    const p = makeProject({ sourceStatuses: ['diarisation_pending'] })
    const states = stageStates(p)
    expect(states.upload).toBe('done')
    expect(states.speaker).toBe('needs_you')
    expect(states.separate).toBe('upcoming')
    expect(states.match).toBe('upcoming')
    expect(states.transcribe).toBe('upcoming')
    expect(states.review).toBe('upcoming')
    expect(states.export).toBe('upcoming')
  })

  it('marks the current step active while jobs run', () => {
    const p = makeProject({
      sourceStatuses: ['separation_running'],
      activeJobs: [{ type: 'vocal_separation' }],
      referencePath: '/data/ref.wav',
    })
    const states = stageStates(p)
    expect(states.separate).toBe('active')
    expect(states.match).toBe('upcoming')
  })

  it('marks separate done once the pipeline reaches match', () => {
    const p = makeProject({
      sourceStatuses: ['diarisation_running'],
      activeJobs: [{ type: 'diarisation' }],
      referencePath: '/data/ref.wav',
    })
    const states = stageStates(p)
    expect(states.separate).toBe('done')
    expect(states.match).toBe('active')
  })

  it('marks speaker active while separation runs for the scan', () => {
    const p = makeProject({
      sourceStatuses: ['separation_running'],
      activeJobs: [{ type: 'vocal_separation' }],
    })
    expect(stageStates(p).speaker).toBe('active')
  })
})

describe('stepChip', () => {
  it('shows Not run yet before the pipeline starts', () => {
    const p = makeProject({ sourceStatuses: ['diarisation_pending'] })
    expect(stepChip(p, 'separate')).toEqual({ label: 'Not run yet', tone: 'grey' })
    expect(stepChip(p, 'transcribe')).toEqual({ label: 'Not run yet', tone: 'grey' })
  })

  it('shows Running for the active step and Done for completed ones', () => {
    const p = makeProject({
      sourceStatuses: ['diarisation_running'],
      activeJobs: [{ type: 'diarisation' }],
      referencePath: '/data/ref.wav',
    })
    expect(stepChip(p, 'separate')).toEqual({ label: 'Done', tone: 'green' })
    expect(stepChip(p, 'match')).toEqual({ label: 'Running', tone: 'blue' })
  })

  it('shows Ready when the current step is waiting on the user', () => {
    const p = makeProject({
      sourceStatuses: ['separation_pending'],
      referencePath: '/data/ref.wav',
    })
    expect(stepChip(p, 'separate')).toEqual({ label: 'Ready', tone: 'amber' })
  })

  it('overrides with Failed when a source failed that step', () => {
    const pSep = makeProject({ sourceStatuses: ['separation_failed'], referencePath: '/data/ref.wav' })
    expect(stepChip(pSep, 'separate')).toEqual({ label: 'Failed', tone: 'red' })

    const pMatch = makeProject({ sourceStatuses: ['diarisation_failed'], referencePath: '/data/ref.wav' })
    expect(stepChip(pMatch, 'match')).toEqual({ label: 'Failed', tone: 'red' })
  })

  it('marks all steps Done once the project reaches review', () => {
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      pendingCount: 5,
    })
    expect(stepChip(p, 'separate')).toEqual({ label: 'Done', tone: 'green' })
    expect(stepChip(p, 'match')).toEqual({ label: 'Done', tone: 'green' })
    expect(stepChip(p, 'transcribe')).toEqual({ label: 'Done', tone: 'green' })
  })
})

describe('XTTS terminal stage', () => {
  it('stagesFor swaps the terminal chip', () => {
    expect(stagesFor(false)).toEqual([
      'upload', 'speaker', 'separate', 'match', 'transcribe', 'review', 'export',
    ])
    expect(stagesFor(true)).toEqual([
      'upload', 'speaker', 'separate', 'match', 'transcribe', 'review', 'train',
    ])
  })

  it('returns train (not export) at the terminal when XTTS is enabled', () => {
    const p = makeProject({ sourceStatuses: ['complete'], referencePath: '/data/ref.wav' })
    expect(deriveStage(p, true)).toBe('train')
    expect(deriveStage(p, false)).toBe('export')
  })

  it('does not affect earlier stages when XTTS is enabled', () => {
    const p = makeProject({
      sourceStatuses: ['complete'],
      referencePath: '/data/ref.wav',
      pendingCount: 3,
    })
    expect(deriveStage(p, true)).toBe('review')
  })

  it('marks the train chip needs_you at the terminal', () => {
    const p = makeProject({ sourceStatuses: ['complete'], referencePath: '/data/ref.wav' })
    const states = stageStates(p, true)
    expect(states.review).toBe('done')
    expect(states.train).toBe('needs_you')
  })
})
