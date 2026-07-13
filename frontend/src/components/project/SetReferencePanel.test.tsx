import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SetReferencePanel } from './SetReferencePanel'
import type { ProjectDetail, SourceStatus, JobSummary, SpeakerCandidate } from '../../types/api'
import {
  ApiError,
  startPipeline,
  startScout,
  getScoutStatus,
  selectScoutSpeaker,
  uploadReference,
} from '../../api/client'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    startPipeline: vi.fn(),
    startScout: vi.fn(),
    getScoutStatus: vi.fn(),
    getScoutSampleUrl: vi.fn(
      (_projectId: string, label: string, index: number) => `/api/samples/${label}/${index}`,
    ),
    selectScoutSpeaker: vi.fn(),
    uploadReference: vi.fn(),
  }
})

const mockStartPipeline = vi.mocked(startPipeline)
const mockStartScout = vi.mocked(startScout)
const mockGetScoutStatus = vi.mocked(getScoutStatus)
const mockSelectScoutSpeaker = vi.mocked(selectScoutSpeaker)
const mockUploadReference = vi.mocked(uploadReference)

function makeProject(overrides: {
  sourceStatuses?: SourceStatus[]
  activeJobs?: Partial<JobSummary>[]
  referencePath?: string | null
} = {}): ProjectDetail {
  const { sourceStatuses = ['diarisation_pending'], activeJobs = [], referencePath = null } = overrides
  return {
    id: 'proj-1',
    name: 'Test project',
    status: 'awaiting_reference',
    created_at: '2026-07-13T00:00:00Z',
    updated_at: '2026-07-13T00:00:00Z',
    reference_path: referencePath,
    reference_origin: null,
    config: {
      whisper_model: 'large-v3',
      language: null,
      match_threshold: 0.5,
      target_duration_secs: 600,
      auto_approve_enabled: true,
      auto_approve_match_threshold: 0.85,
      auto_approve_transcript_threshold: 0.9,
      whisper_batch_size: 16,
      whisper_compute_type: 'default',
      demucs_model: 'htdemucs_ft',
      align_words: false,
    },
    active_jobs: activeJobs.map((j, i) => ({
      id: j.id ?? `j${i}`,
      type: j.type ?? 'vocal_separation',
      status: j.status ?? 'running',
      progress: j.progress ?? 40,
    })),
    recent_failed_jobs: [],
    stats: {
      approved_count: 0,
      approved_duration_secs: 0,
      pending_count: 0,
      total_segments: 0,
      auto_approved_count: 0,
      maybe_count: 0,
      rejected_count: 0,
      below_threshold_count: 0,
      source_coverage: sourceStatuses.map((s, i) => ({
        source_id: `src-${i + 1}`,
        filename: `s01e0${i + 1}.mkv`,
        status: s,
        coverage_ratio: 0,
        low_coverage_warning: false,
        error: null,
      })),
    },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  // Default: no prior scout has run.
  mockGetScoutStatus.mockRejectedValue(new ApiError('no_scout', 'No scout has been run.', null))
})

// Build a candidate with a pool from a list of turn durations.
function cand(label: string, totalSecs: number, segmentCount: number, durations: number[]): SpeakerCandidate {
  let t = 0
  const pool = durations.map((d, i) => {
    const turn = { index: i, start: t, end: t + d, duration: d, sample_url: `${label}/${i}` }
    t += d + 0.5
    return turn
  })
  return { speaker_label: label, total_secs: totalSecs, segment_count: segmentCount, pool }
}

describe('SetReferencePanel — whose-voice prompt', () => {
  it('offers Find speakers and Upload a clip on a queued source', () => {
    render(<SetReferencePanel project={makeProject({ sourceStatuses: ['separation_pending'] })} onAction={vi.fn()} pollIntervalMs={10} />)
    expect(screen.getByText(/whose voice are we after/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Find speakers' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Upload a clip' })).toBeInTheDocument()
  })

  it('starts the pipeline and refetches when Find speakers is clicked', async () => {
    const user = userEvent.setup()
    const onAction = vi.fn()
    mockStartPipeline.mockResolvedValue({ enqueued_jobs: [] })

    render(<SetReferencePanel project={makeProject({ sourceStatuses: ['separation_pending'] })} onAction={onAction} pollIntervalMs={10} />)

    await user.click(screen.getByRole('button', { name: 'Find speakers' }))
    await waitFor(() => expect(mockStartPipeline).toHaveBeenCalledWith('proj-1'))
    expect(onAction).toHaveBeenCalled()
    expect(mockStartScout).not.toHaveBeenCalled()
  })

  it('uploads a reference clip via the Upload a clip button', async () => {
    const user = userEvent.setup()
    const onAction = vi.fn()
    mockUploadReference.mockResolvedValue({ reference_path: 'ref.wav', duration_secs: 12 })

    const { container } = render(
      <SetReferencePanel project={makeProject({ sourceStatuses: ['separation_pending'] })} onAction={onAction} pollIntervalMs={10} />,
    )

    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['x'], 'voice.wav', { type: 'audio/wav' })
    await user.upload(input, file)

    await waitFor(() => expect(mockUploadReference).toHaveBeenCalled())
    expect(mockUploadReference.mock.calls[0][0]).toBe('proj-1')
    expect(mockUploadReference.mock.calls[0][1]).toBe(file)
    expect(onAction).toHaveBeenCalled()
  })
})

describe('SetReferencePanel — preparing / separating / failed', () => {
  it('shows a preparing message while the audio is still extracting', () => {
    render(<SetReferencePanel project={makeProject({ sourceStatuses: ['extracting'] })} onAction={vi.fn()} pollIntervalMs={10} />)
    expect(screen.getByText(/getting your video ready/i)).toBeInTheDocument()
  })

  it('shows separation progress while separation runs for the scan', () => {
    render(
      <SetReferencePanel
        project={makeProject({
          sourceStatuses: ['separation_running'],
          activeJobs: [{ type: 'vocal_separation', progress: 55 }],
        })}
        onAction={vi.fn()}
        pollIntervalMs={10}
      />,
    )
    expect(screen.getByText(/finding the speakers/i)).toBeInTheDocument()
    expect(screen.getByText('Separating vocals')).toBeInTheDocument()
  })

  it('points to the retry alert when separation failed before a reference', () => {
    render(<SetReferencePanel project={makeProject({ sourceStatuses: ['separation_failed'] })} onAction={vi.fn()} pollIntervalMs={10} />)
    expect(screen.getByText(/something went wrong/i)).toBeInTheDocument()
    expect(screen.getByText(/retry it from the alert below/i)).toBeInTheDocument()
  })
})

describe('SetReferencePanel — scan and pick', () => {
  it('auto-scans the first ready source when no prior scan exists', async () => {
    mockStartScout.mockResolvedValue({ job_id: 'job-1', type: 'scout_speakers' })
    // mount fetch rejects (no_scout, from beforeEach); the poll returns complete.
    mockGetScoutStatus.mockRejectedValueOnce(new ApiError('no_scout', 'none', null))
    mockGetScoutStatus.mockResolvedValue({
      status: 'complete',
      source_id: 'src-1',
      speakers: [
        cand('SPEAKER_01', 88.2, 41, [30]),
        cand('SPEAKER_00', 412.6, 173, [30]),
      ],
    })

    render(<SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />)

    await waitFor(() => expect(mockStartScout).toHaveBeenCalledWith('proj-1', 'src-1', undefined))
    await waitFor(() => expect(screen.getByText('SPEAKER_00')).toBeInTheDocument())

    // Sorted by talk time descending.
    const labels = screen.getAllByText(/^SPEAKER_/).map((el) => el.textContent)
    expect(labels).toEqual(['SPEAKER_00', 'SPEAKER_01'])
  })

  it('renders existing candidates on mount without re-scanning', async () => {
    mockGetScoutStatus.mockResolvedValue({
      status: 'complete',
      source_id: 'src-1',
      speakers: [cand('SPEAKER_02', 200, 60, [30])],
    })

    render(<SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />)

    await waitFor(() => expect(screen.getByText('SPEAKER_02')).toBeInTheDocument())
    expect(mockStartScout).not.toHaveBeenCalled()
  })

  it('disables "Use this voice" for candidates under the 5-second floor', async () => {
    mockGetScoutStatus.mockResolvedValue({
      status: 'complete',
      source_id: 'src-1',
      speakers: [
        cand('SPEAKER_00', 120, 30, [30]),
        cand('SPEAKER_01', 3.2, 2, [3.2]), // only 3.2s of audio → under the 5s floor
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
      speakers: [cand('SPEAKER_00', 412.6, 173, [30])],
    })

    render(<SetReferencePanel project={makeProject()} onAction={onAction} pollIntervalMs={10} />)

    await waitFor(() => expect(screen.getByText('SPEAKER_00')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Use this voice' }))

    await waitFor(() => expect(mockSelectScoutSpeaker).toHaveBeenCalledWith('proj-1', 'SPEAKER_00', []))
    expect(onAction).toHaveBeenCalled()
  })

  it('excludes a wrong-voice turn and sends the exclusion on select', async () => {
    const user = userEvent.setup()
    mockSelectScoutSpeaker.mockResolvedValue({ reference_path: 'reference.wav', duration_secs: 10 })
    mockGetScoutStatus.mockResolvedValue({
      status: 'complete',
      source_id: 'src-1',
      speakers: [cand('SPEAKER_00', 200, 5, [12, 10])],
    })

    render(<SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />)

    await waitFor(() => expect(screen.getByText('SPEAKER_00')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Choose segments' }))

    // Two pool turns → two "Not this speaker" checkboxes. Exclude the first.
    const checkboxes = screen.getAllByRole('checkbox')
    expect(checkboxes).toHaveLength(2)
    await user.click(checkboxes[0])

    await user.click(screen.getByRole('button', { name: 'Use this voice' }))
    await waitFor(() =>
      expect(mockSelectScoutSpeaker).toHaveBeenCalledWith('proj-1', 'SPEAKER_00', [0]),
    )
  })

  it('re-scans with the expected speaker count from the advanced drawer', async () => {
    const user = userEvent.setup()
    mockStartScout.mockResolvedValue({ job_id: 'j', type: 'scout_speakers' })
    mockGetScoutStatus.mockResolvedValue({
      status: 'complete',
      source_id: 'src-1',
      speakers: [cand('SPEAKER_00', 120, 30, [30])],
    })

    render(<SetReferencePanel project={makeProject()} onAction={vi.fn()} pollIntervalMs={10} />)

    await waitFor(() => expect(screen.getByText('SPEAKER_00')).toBeInTheDocument())

    fireEvent.change(screen.getByLabelText('Expected number of speakers'), { target: { value: '2' } })
    await user.click(screen.getByRole('button', { name: 'Scan again' }))

    await waitFor(() => expect(mockStartScout).toHaveBeenCalledWith('proj-1', 'src-1', 2))
  })
})
