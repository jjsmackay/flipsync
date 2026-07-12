import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { SetReferencePanel } from './SetReferencePanel'
import type { ProjectDetail, ScoutStatus, SourceStatus } from '../../types/api'
import { getScoutStatus } from '../../api/client'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    startScout: vi.fn(),
    getScoutStatus: vi.fn(),
    getScoutSampleUrl: vi.fn(
      (_projectId: string, label: string, index: number) => `/api/samples/${label}/${index}`,
    ),
    selectScoutSpeaker: vi.fn(),
    continuePipeline: vi.fn(),
    uploadReference: vi.fn(),
  }
})

const mockGetScoutStatus = vi.mocked(getScoutStatus)

function makeProject(overrides: Partial<ProjectDetail> = {}): ProjectDetail {
  const sources: { source_id: string; filename: string; status: SourceStatus }[] = [
    { source_id: 'src-1', filename: 's01e01.mkv', status: 'diarisation_pending' },
  ]
  return {
    id: 'proj-1',
    name: 'Test project',
    status: 'awaiting_reference',
    created_at: '2026-07-13T00:00:00Z',
    updated_at: '2026-07-13T00:00:00Z',
    stats: {
      approved_count: 0,
      approved_duration_secs: 0,
      pending_count: 0,
      total_segments: 0,
      auto_approved_count: 0,
      maybe_count: 0,
      rejected_count: 0,
      below_threshold_count: 0,
      source_coverage: sources.map((s) => ({
        ...s,
        coverage_ratio: 0,
        low_coverage_warning: false,
        error: null,
      })),
    },
    config: {
      whisper_model: 'large-v3',
      language: null,
      match_threshold: 0.5,
      target_duration_secs: 600,
      auto_approve_enabled: true,
      auto_approve_match_threshold: 0.85,
      auto_approve_transcript_threshold: 0.9,
    },
    active_jobs: [],
    recent_failed_jobs: [],
    reference_path: null,
    reference_origin: null,
    ...overrides,
  }
}

const RUNNING: ScoutStatus = { status: 'running', progress: 40, source_id: 'src-1', speakers: [] }
const COMPLETE: ScoutStatus = {
  status: 'complete',
  source_id: 'src-1',
  speakers: [
    {
      speaker_label: 'SPEAKER_00',
      total_secs: 120,
      segment_count: 30,
      pool: [{ index: 0, start: 0, end: 120, duration: 120, sample_url: 'a/0' }],
    },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('SetReferencePanel poll resilience', () => {
  it('keeps polling through a failed poll and still lands the completed scan', async () => {
    mockGetScoutStatus
      .mockResolvedValueOnce(RUNNING) // mount fetch
      .mockRejectedValueOnce(new Error('network down')) // first poll fails
      .mockResolvedValue(COMPLETE) // subsequent polls succeed

    render(<SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />)

    // If a single failed poll stopped the loop, the speakers would never appear.
    await waitFor(() => expect(screen.getByText('SPEAKER_00')).toBeInTheDocument())
    expect(mockGetScoutStatus.mock.calls.length).toBeGreaterThanOrEqual(3)
  })

  it('shows a transient retrying note while polls fail, without wedging the scan state', async () => {
    mockGetScoutStatus
      .mockResolvedValueOnce(RUNNING) // mount fetch
      .mockRejectedValue(new Error('network down')) // every poll fails

    render(<SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />)

    await waitFor(() => expect(screen.getByText(/Connection lost — retrying/)).toBeInTheDocument())
    // Still presented as scanning — not failed, not idle.
    expect(screen.getByText(/Scanning for speakers…/)).toBeInTheDocument()
  })

  it('shows the failure banner AND prior still-pickable candidates when the latest scan failed', async () => {
    // Worker C's change: GET /reference/scout returns prior candidates alongside
    // a failed latest scan.
    mockGetScoutStatus.mockResolvedValue({
      status: 'failed',
      source_id: 'src-1',
      error: 'GPU out of memory',
      speakers: [
        {
          speaker_label: 'SPEAKER_00',
          total_secs: 300,
          segment_count: 80,
          pool: [{ index: 0, start: 0, end: 300, duration: 300, sample_url: 'a/0' }],
        },
      ],
    })

    render(<SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />)

    await waitFor(() => expect(screen.getByText(/Scan failed: GPU out of memory/)).toBeInTheDocument())
    expect(screen.getByText(/last successful scan are still available/)).toBeInTheDocument()

    const card = screen.getByText('SPEAKER_00').closest('li') as HTMLElement
    expect(within(card).getByRole('button', { name: 'Use this voice' })).toBeEnabled()
  })

  it('failed scan with no prior candidates shows only the banner', async () => {
    mockGetScoutStatus.mockResolvedValue({
      status: 'failed',
      source_id: 'src-1',
      error: 'boom',
      speakers: [],
    })

    render(<SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />)

    await waitFor(() => expect(screen.getByText(/Scan failed: boom/)).toBeInTheDocument())
    expect(screen.queryByText(/still available below/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Use this voice' })).not.toBeInTheDocument()
  })
})
