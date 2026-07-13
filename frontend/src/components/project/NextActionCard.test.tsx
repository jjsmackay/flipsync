import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { NextActionCard } from './NextActionCard'
import type { ProjectDetail, SourceStatus, JobSummary } from '../../types/api'
import { TUNING_DEFAULTS } from '../../utils/tuning'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    startPipeline: vi.fn(),
    continuePipeline: vi.fn(),
    runTranscription: vi.fn(),
    getScoutStatus: vi.fn(() => new Promise(() => {})),
    startScout: vi.fn(),
    selectScoutSpeaker: vi.fn(),
    uploadReference: vi.fn(),
    triggerExport: vi.fn(),
    getSegments: vi.fn(),
  }
})

function makeProject(overrides: {
  sourceStatuses?: SourceStatus[]
  activeJobs?: Partial<JobSummary>[]
  referencePath?: string | null
  pendingCount?: number
  approvedCount?: number
  belowThresholdCount?: number
  status?: ProjectDetail['status']
}): ProjectDetail {
  const {
    sourceStatuses = [],
    activeJobs = [],
    referencePath = null,
    pendingCount = 0,
    approvedCount = 0,
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
      progress: j.progress ?? 40,
    })),
    recent_failed_jobs: [],
    stats: {
      approved_count: approvedCount,
      approved_duration_secs: 0,
      pending_count: pendingCount,
      maybe_count: 0,
      total_segments: pendingCount + approvedCount + belowThresholdCount,
      auto_approved_count: 0,
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

function renderCard(project: ProjectDetail, onOpenSettings?: () => void) {
  return render(
    <MemoryRouter>
      <NextActionCard project={project} onAction={() => {}} onOpenSettings={onOpenSettings} />
    </MemoryRouter>,
  )
}

describe('NextActionCard', () => {
  it('prompts for an upload on an empty project', () => {
    renderCard(makeProject({}))
    expect(screen.getByText(/upload a video to get started/i)).toBeInTheDocument()
  })

  it('prompts whose voice on a freshly-uploaded source', () => {
    renderCard(makeProject({ sourceStatuses: ['separation_pending'] }))
    expect(screen.getByText(/whose voice are we after/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /find speakers/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /upload a clip/i })).toBeInTheDocument()
  })

  it('shows the scan at the reference gate with no reference', () => {
    renderCard(makeProject({ sourceStatuses: ['diarisation_pending'] }))
    expect(screen.getByText(/whose voice are we after/i)).toBeInTheDocument()
    expect(screen.getByText(/scanning for speakers/i)).toBeInTheDocument()
  })

  it('keeps the Speaker stage while separation runs for the scan', () => {
    renderCard(
      makeProject({
        sourceStatuses: ['separation_running'],
        activeJobs: [{ type: 'vocal_separation', progress: 40 }],
      }),
    )
    expect(screen.getByText(/finding the speakers/i)).toBeInTheDocument()
    expect(screen.getByText('Separating vocals')).toBeInTheDocument()
  })

  it('offers Start processing for an uploaded-clip source queued to start', () => {
    renderCard(makeProject({ sourceStatuses: ['separation_pending'], referencePath: '/data/ref.wav' }))
    expect(screen.getByRole('button', { name: /start processing/i })).toBeInTheDocument()
  })

  it('offers Continue processing at the gate with a reference set', () => {
    renderCard(
      makeProject({ sourceStatuses: ['diarisation_pending'], referencePath: '/data/ref.wav' }),
    )
    expect(screen.getByRole('button', { name: /continue processing/i })).toBeInTheDocument()
  })

  it('links to the review queue when segments await review', () => {
    renderCard(makeProject({ sourceStatuses: ['complete'], referencePath: '/data/ref.wav', pendingCount: 12 }))
    expect(screen.getByText(/12 segments ready to review/i)).toBeInTheDocument()
    const link = screen.getByRole('link', { name: /start reviewing/i })
    expect(link).toHaveAttribute('href', '/projects/p1/review')
  })

  it('shows the export action once review is done', () => {
    renderCard(makeProject({ sourceStatuses: ['complete'], referencePath: '/data/ref.wav', approvedCount: 8 }))
    expect(screen.getByText(/ready to export/i)).toBeInTheDocument()
  })

  it('guides to lower the threshold when every segment is below it', async () => {
    const onOpenSettings = vi.fn()
    renderCard(
      makeProject({
        sourceStatuses: ['complete'],
        referencePath: '/data/ref.wav',
        belowThresholdCount: 12,
      }),
      onOpenSettings,
    )
    // Not the misleading "ready to export" / "ready to review" copy.
    expect(screen.queryByText(/ready to export/i)).not.toBeInTheDocument()
    expect(screen.getByText(/below your match threshold/i)).toBeInTheDocument()
    const btn = screen.getByRole('button', { name: /adjust threshold/i })
    const { default: userEvent } = await import('@testing-library/user-event')
    await userEvent.setup().click(btn)
    expect(onOpenSettings).toHaveBeenCalled()
  })
})
