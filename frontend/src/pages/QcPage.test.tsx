import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QcPage } from './QcPage'
import type { ProjectDetail, Segment } from '../types/api'
import { TUNING_DEFAULTS } from '../utils/tuning'
import {
  getProject,
  getSegments,
  createTuningPreview,
  getTuningPreview,
} from '../api/client'
import { EXPORTABLE_STATUSES_CSV } from '../constants'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    getProject: vi.fn(),
    getSegments: vi.fn(),
    createTuningPreview: vi.fn(),
    getTuningPreview: vi.fn(),
    getSegmentAudioUrl: vi.fn((p: string, s: string) => `/api/raw/${p}/${s}`),
    getTuningPreviewAudioUrl: vi.fn((p: string, id: string) => `/api/clean/${p}/${id}`),
  }
})

const mockGetProject = vi.mocked(getProject)
const mockGetSegments = vi.mocked(getSegments)
const mockCreateTuningPreview = vi.mocked(createTuningPreview)
const mockGetTuningPreview = vi.mocked(getTuningPreview)

function makeProject(): ProjectDetail {
  return {
    id: 'proj-1',
    name: 'Test project',
    status: 'review',
    created_at: '2026-07-14T00:00:00Z',
    updated_at: '2026-07-14T00:00:00Z',
    reference_path: 'reference.wav',
    reference_origin: null,
    reference_transcript: null,
    config: {
      whisper_model: 'large-v3',
      language: null,
      match_threshold: 0.5,
      target_duration_secs: 600,
      auto_approve_enabled: true,
      auto_approve_match_threshold: 0.85,
      auto_approve_transcript_threshold: 0.9,
      ...TUNING_DEFAULTS,
    },
    active_jobs: [],
    recent_failed_jobs: [],
    stats: {
      approved_count: 1,
      approved_duration_secs: 10,
      pending_count: 0,
      total_segments: 1,
      auto_approved_count: 0,
      maybe_count: 0,
      rejected_count: 0,
      below_threshold_count: 0,
      source_coverage: [],
    },
  }
}

function makeSegment(over: Partial<Segment> = {}): Segment {
  return {
    id: 'seg-1',
    source_id: 'src-1',
    source_filename: 's01e01.mkv',
    start_secs: 0,
    end_secs: 10,
    duration_secs: 10,
    match_confidence: 0.9,
    transcript: 'hello there',
    transcript_edited: null,
    transcript_confidence: 0.9,
    status: 'approved',
    clipping_warning: false,
    flags: null,
    audio_url: '/api/raw/proj-1/seg-1',
    ...over,
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/projects/proj-1/qc']}>
      <Routes>
        <Route path="/projects/:projectId/qc" element={<QcPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockGetProject.mockResolvedValue(makeProject())
  mockGetSegments.mockResolvedValue({ segments: [makeSegment()], total: 1, page: 1, per_page: 200 } as never)
  mockGetTuningPreview.mockResolvedValue({ id: 'prev-1', status: 'generating', error: null } as never)
  mockCreateTuningPreview.mockResolvedValue({ enqueued_job: { id: 'prev-1', type: 'tuning_preview' } })
})

describe('QcPage', () => {
  it('requests only the export set of segments', async () => {
    renderPage()
    await waitFor(() =>
      expect(mockGetSegments).toHaveBeenCalledWith(
        'proj-1',
        expect.objectContaining({ status: EXPORTABLE_STATUSES_CSV }),
      ),
    )
  })

  it('shows the before/after toggle for the selected segment', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Before (raw)' })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'After clean' })).toBeInTheDocument()
  })

  it('starts a cleanup preview for the segment when After clean is opened', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(screen.getByRole('button', { name: 'After clean' })).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'After clean' }))

    await waitFor(() =>
      expect(mockCreateTuningPreview).toHaveBeenCalledWith(
        'proj-1',
        expect.objectContaining({
          stage: 'cleanup',
          target: { segment_id: 'seg-1' },
          params: expect.objectContaining({ target_lufs: expect.any(Number) }),
        }),
      ),
    )
  })

  it('shows an empty-state message when there are no approved segments', async () => {
    mockGetSegments.mockResolvedValue({ segments: [], total: 0, page: 1, per_page: 200 } as never)
    renderPage()
    await waitFor(() => expect(screen.getByText(/No approved segments yet/)).toBeInTheDocument())
  })
})
