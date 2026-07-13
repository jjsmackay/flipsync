import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ReferenceCard } from './ReferenceCard'
import type { ProjectDetail, SourceStatus, ReferenceOrigin } from '../../types/api'
import { TUNING_DEFAULTS } from '../../utils/tuning'
import {
  ApiError,
  getScoutStatus,
  selectScoutSpeaker,
  uploadReference,
  getReferenceAudioUrl,
} from '../../api/client'

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
    uploadReference: vi.fn(),
  }
})

const mockGetScoutStatus = vi.mocked(getScoutStatus)
const mockSelectScoutSpeaker = vi.mocked(selectScoutSpeaker)
const mockUploadReference = vi.mocked(uploadReference)

function makeProject(overrides: {
  sourceStatuses?: SourceStatus[]
  referencePath?: string | null
  referenceOrigin?: ReferenceOrigin | null
} = {}): ProjectDetail {
  const {
    sourceStatuses = ['complete'],
    referencePath = 'reference.wav',
    referenceOrigin = null,
  } = overrides
  return {
    id: 'proj-1',
    name: 'Test project',
    status: 'review',
    created_at: '2026-07-14T00:00:00Z',
    updated_at: '2026-07-14T00:00:00Z',
    reference_path: referencePath,
    reference_origin: referenceOrigin,
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
  // No scan mounted by default — most tests don't need the picker's own fetch.
  mockGetScoutStatus.mockRejectedValue(new ApiError('no_scout', 'No scout has been run.', null))
})

// ReferenceCard assumes reference_path is set — the caller (ProjectDashboardPage)
// only mounts it behind `project.reference_path && <ReferenceCard .../>`, so
// there's no "hidden" state to render here.
describe('ReferenceCard — provenance', () => {
  it('shows a generic label when origin is null', () => {
    render(<ReferenceCard project={makeProject({ referenceOrigin: null })} onAction={vi.fn()} />)
    expect(screen.getByText('Reference clip')).toBeInTheDocument()
  })

  it('shows "Uploaded clip" for an uploaded origin', () => {
    render(
      <ReferenceCard
        project={makeProject({ referenceOrigin: { type: 'uploaded' } })}
        onAction={vi.fn()}
      />,
    )
    expect(screen.getByText('Uploaded clip')).toBeInTheDocument()
  })

  it('shows the picked speaker for a diarise_pick origin', () => {
    render(
      <ReferenceCard
        project={makeProject({
          referenceOrigin: { type: 'diarise_pick', source_id: 'src-1', speaker_label: 'SPEAKER_01' },
        })}
        onAction={vi.fn()}
      />,
    )
    expect(screen.getByText('Picked SPEAKER_01 from a scan')).toBeInTheDocument()
  })
})

describe('ReferenceCard — audio', () => {
  it('points the audio element at the reference audio URL, versioned on updated_at', () => {
    const { container } = render(<ReferenceCard project={makeProject()} onAction={vi.fn()} />)
    const audio = container.querySelector('audio')
    const src = audio?.getAttribute('src') ?? ''
    expect(src.startsWith(`${getReferenceAudioUrl('proj-1')}?v=`)).toBe(true)
  })
})

describe('ReferenceCard — replace with upload', () => {
  it('uploads a new clip and calls onAction', async () => {
    const user = userEvent.setup()
    const onAction = vi.fn()
    mockUploadReference.mockResolvedValue({ reference_path: 'ref2.wav', duration_secs: 15 })

    const { container } = render(<ReferenceCard project={makeProject()} onAction={onAction} />)

    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['x'], 'voice.wav', { type: 'audio/wav' })
    await user.upload(input, file)

    await waitFor(() => expect(mockUploadReference).toHaveBeenCalledWith('proj-1', file, expect.any(Function)))
    expect(onAction).toHaveBeenCalled()
  })
})

describe('ReferenceCard — pick from scan', () => {
  it('expands the picker and calls onAction after a successful select', async () => {
    const user = userEvent.setup()
    const onAction = vi.fn()
    mockSelectScoutSpeaker.mockResolvedValue({ reference_path: 'reference.wav', duration_secs: 27.9 })
    mockGetScoutStatus.mockResolvedValue({
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
    })

    render(<ReferenceCard project={makeProject()} onAction={onAction} />)

    expect(screen.queryByText('SPEAKER_00')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Pick from scan' }))

    await waitFor(() => expect(screen.getByText('SPEAKER_00')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Use this voice' }))

    await waitFor(() => expect(mockSelectScoutSpeaker).toHaveBeenCalledWith('proj-1', 'SPEAKER_00', []))
    expect(onAction).toHaveBeenCalled()
  })
})
