import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SetReferencePanel } from './SetReferencePanel'
import type { ProjectDetail, SourceStatus } from '../../types/api'
import {
  ApiError,
  startScout,
  getScoutStatus,
  selectScoutSpeaker,
  continuePipeline,
} from '../../api/client'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    startScout: vi.fn(),
    getScoutStatus: vi.fn(),
    getScoutSampleUrl: vi.fn((_projectId: string, label: string) => `/api/samples/${label}`),
    selectScoutSpeaker: vi.fn(),
    continuePipeline: vi.fn(),
    uploadReference: vi.fn(),
  }
})

const mockStartScout = vi.mocked(startScout)
const mockGetScoutStatus = vi.mocked(getScoutStatus)
const mockSelectScoutSpeaker = vi.mocked(selectScoutSpeaker)
const mockContinuePipeline = vi.mocked(continuePipeline)

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

beforeEach(() => {
  vi.clearAllMocks()
  // Default: no prior scout has run.
  mockGetScoutStatus.mockRejectedValue(new ApiError('no_scout', 'No scout has been run.', null))
})

describe('SetReferencePanel', () => {
  it('renders both tabs with Find speakers active by default', async () => {
    render(<SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />)

    expect(screen.getByRole('button', { name: 'Find speakers' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Upload' })).toBeInTheDocument()
    // Find-speakers content (the scan control) is visible; Upload content is not.
    expect(screen.getByRole('button', { name: 'Scan for speakers' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Upload reference clip' })).not.toBeInTheDocument()

    await waitFor(() => expect(mockGetScoutStatus).toHaveBeenCalled())
  })

  it('scans, polls, and renders speaker cards sorted by talk time descending', async () => {
    const user = userEvent.setup()
    mockStartScout.mockResolvedValue({ job_id: 'job-1', type: 'scout_speakers' })
    // Mount fetch rejects (no_scout, from beforeEach); the first poll returns complete.
    mockGetScoutStatus.mockRejectedValueOnce(new ApiError('no_scout', 'none', null))
    mockGetScoutStatus.mockResolvedValue({
      status: 'complete',
      source_id: 'src-1',
      speakers: [
        { speaker_label: 'SPEAKER_01', total_secs: 88.2, segment_count: 41, sample_url: 'x' },
        { speaker_label: 'SPEAKER_00', total_secs: 412.6, segment_count: 173, sample_url: 'y' },
      ],
    })

    render(<SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />)

    await user.click(screen.getByRole('button', { name: 'Scan for speakers' }))
    expect(mockStartScout).toHaveBeenCalledWith('proj-1', 'src-1')

    await waitFor(() => expect(screen.getByText('SPEAKER_00')).toBeInTheDocument())

    const labels = screen.getAllByText(/^SPEAKER_/).map((el) => el.textContent)
    expect(labels).toEqual(['SPEAKER_00', 'SPEAKER_01'])
  })

  it('disables "Use this voice" for candidates under the 5-second floor', async () => {
    mockGetScoutStatus.mockResolvedValue({
      status: 'complete',
      source_id: 'src-1',
      speakers: [
        { speaker_label: 'SPEAKER_00', total_secs: 120, segment_count: 30, sample_url: 'a' },
        { speaker_label: 'SPEAKER_01', total_secs: 3.2, segment_count: 2, sample_url: 'b' },
      ],
    })

    render(<SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />)

    await waitFor(() => expect(screen.getByText('SPEAKER_01')).toBeInTheDocument())

    const longCard = screen.getByText('SPEAKER_00').closest('li') as HTMLElement
    const shortCard = screen.getByText('SPEAKER_01').closest('li') as HTMLElement
    expect(within(longCard).getByRole('button', { name: 'Use this voice' })).toBeEnabled()
    expect(within(shortCard).getByRole('button', { name: 'Use this voice' })).toBeDisabled()
  })

  it('selects a speaker and calls the client and onAction', async () => {
    const user = userEvent.setup()
    const onAction = vi.fn()
    mockSelectScoutSpeaker.mockResolvedValue({ reference_path: 'reference.wav', duration_secs: 27.9 })
    mockGetScoutStatus.mockResolvedValue({
      status: 'complete',
      source_id: 'src-1',
      speakers: [
        { speaker_label: 'SPEAKER_00', total_secs: 412.6, segment_count: 173, sample_url: 'y' },
      ],
    })

    render(<SetReferencePanel project={makeProject()} onAction={onAction} pollIntervalMs={10} />)

    await waitFor(() => expect(screen.getByText('SPEAKER_00')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Use this voice' }))

    await waitFor(() => expect(mockSelectScoutSpeaker).toHaveBeenCalledWith('proj-1', 'SPEAKER_00'))
    expect(onAction).toHaveBeenCalled()
  })

  it('renders existing candidates on mount without a scan', async () => {
    mockGetScoutStatus.mockResolvedValue({
      status: 'complete',
      source_id: 'src-1',
      speakers: [
        { speaker_label: 'SPEAKER_02', total_secs: 200, segment_count: 60, sample_url: 'z' },
      ],
    })

    render(<SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />)

    await waitFor(() => expect(screen.getByText('SPEAKER_02')).toBeInTheDocument())
    expect(mockStartScout).not.toHaveBeenCalled()
  })

  it('disables Continue without a reference and enables it once set', async () => {
    const { rerender } = render(
      <SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />,
    )
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled()

    rerender(
      <SetReferencePanel
        project={makeProject({
          reference_path: 'reference.wav',
          reference_origin: { type: 'diarise_pick', source_id: 'src-1', speaker_label: 'SPEAKER_00' },
        })}
        onAction={vi.fn()}
        pollIntervalMs={10}
      />,
    )
    expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled()
    expect(screen.getByText('Reference: SPEAKER_00 from s01e01.mkv')).toBeInTheDocument()
  })

  it('calls continuePipeline and onAction when Continue is clicked', async () => {
    const user = userEvent.setup()
    const onAction = vi.fn()
    mockContinuePipeline.mockResolvedValue({ enqueued_jobs: [] })

    render(
      <SetReferencePanel
        project={makeProject({ reference_path: 'reference.wav', reference_origin: { type: 'uploaded' } })}
        onAction={onAction}
        pollIntervalMs={10}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await waitFor(() => expect(mockContinuePipeline).toHaveBeenCalledWith('proj-1'))
    expect(onAction).toHaveBeenCalled()
    expect(screen.getByText('Reference: uploaded clip')).toBeInTheDocument()
  })
})
