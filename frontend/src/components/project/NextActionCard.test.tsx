import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { NextActionCard } from './NextActionCard'
import type { ProjectDetail, SourceStatus, JobSummary } from '../../types/api'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    startPipeline: vi.fn(),
    continuePipeline: vi.fn(),
    runTranscription: vi.fn(),
    getScoutStatus: vi.fn(() => new Promise(() => {})),
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
  status?: ProjectDetail['status']
}): ProjectDetail {
  const {
    sourceStatuses = [],
    activeJobs = [],
    referencePath = null,
    pendingCount = 0,
    approvedCount = 0,
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
      progress: j.progress ?? 40,
    })),
    recent_failed_jobs: [],
    stats: {
      approved_count: approvedCount,
      approved_duration_secs: 0,
      pending_count: pendingCount,
      maybe_count: 0,
      total_segments: pendingCount + approvedCount,
      auto_approved_count: 0,
      rejected_count: 0,
      below_threshold_count: 0,
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

function renderCard(project: ProjectDetail) {
  return render(
    <MemoryRouter>
      <NextActionCard project={project} onAction={() => {}} />
    </MemoryRouter>,
  )
}

describe('NextActionCard', () => {
  it('prompts for an upload on an empty project', () => {
    renderCard(makeProject({}))
    expect(screen.getByText(/upload a video to get started/i)).toBeInTheDocument()
  })

  it('shows the speaker panel at the reference gate', () => {
    renderCard(makeProject({ sourceStatuses: ['diarisation_pending'] }))
    expect(screen.getByText(/whose voice are we after/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /scan for speakers/i })).toBeInTheDocument()
  })

  it('shows job progress with a human label while processing', () => {
    renderCard(
      makeProject({
        sourceStatuses: ['separation_running'],
        activeJobs: [{ type: 'vocal_separation', progress: 40 }],
      }),
    )
    expect(screen.getByText('Separating vocals')).toBeInTheDocument()
  })

  it('offers Start processing for a queued source', () => {
    renderCard(makeProject({ sourceStatuses: ['separation_pending'] }))
    expect(screen.getByRole('button', { name: /start processing/i })).toBeInTheDocument()
  })

  it('offers Continue processing at the gate with a reference set', () => {
    renderCard(
      makeProject({ sourceStatuses: ['diarisation_pending'], referencePath: '/data/ref.wav' }),
    )
    expect(screen.getByRole('button', { name: /continue processing/i })).toBeInTheDocument()
  })

  it('links to the review queue when segments await review', () => {
    renderCard(makeProject({ sourceStatuses: ['complete'], pendingCount: 12 }))
    expect(screen.getByText(/12 segments ready to review/i)).toBeInTheDocument()
    const link = screen.getByRole('link', { name: /start reviewing/i })
    expect(link).toHaveAttribute('href', '/projects/p1/review')
  })

  it('shows the export action once review is done', () => {
    renderCard(makeProject({ sourceStatuses: ['complete'], approvedCount: 8 }))
    expect(screen.getByText(/ready to export/i)).toBeInTheDocument()
  })
})
