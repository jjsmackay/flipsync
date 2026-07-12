import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { SegmentDetail } from './SegmentDetail'
import type { Segment } from '../../types/api'

vi.mock('./WaveformCanvas', () => ({
  WaveformCanvas: () => <div data-testid="waveform" />,
}))
vi.mock('./AudioControls', () => ({
  AudioControls: () => <div data-testid="audio-controls" />,
}))
vi.mock('../../hooks/useAudio', () => ({
  useAudio: () => ({
    isPlaying: false,
    currentTime: 0,
    duration: 0,
    playbackRate: 1,
    play: vi.fn(),
    pause: vi.fn(),
    toggle: vi.fn(),
    seek: vi.fn(),
    setPlaybackRate: vi.fn(),
    restart: vi.fn(),
  }),
}))
vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    getSegmentAudioUrl: vi.fn(() => '/api/audio-url'),
    patchSegment: vi.fn(),
    rerunSegmentTranscription: vi.fn(() => Promise.resolve({})),
  }
})

function makeSegment(overrides: Partial<Segment> = {}): Segment {
  return {
    id: 'seg-1',
    source_id: 'src-1',
    source_filename: 's01e01.mkv',
    start_secs: 10,
    end_secs: 14.5,
    duration_secs: 4.5,
    match_confidence: 0.91,
    transcript: 'Hello there.',
    transcript_edited: null,
    transcript_confidence: 0.88,
    status: 'pending',
    clipping_warning: false,
    flags: null,
    audio_url: '/api/audio-url',
    ...overrides,
  }
}

function renderDetail(segment: Segment) {
  return render(
    <SegmentDetail
      projectId="proj-1"
      segment={segment}
      onStatusChange={vi.fn()}
      onTranscriptChange={vi.fn()}
      onFocusChange={vi.fn()}
      showSpectrogram={false}
      onSpectrogramToggle={vi.fn()}
      autoPlay={false}
    />,
  )
}

const originalCreateObjectURL = URL.createObjectURL
const originalRevokeObjectURL = URL.revokeObjectURL

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => 'blob:mock')
  URL.revokeObjectURL = vi.fn()
})

afterEach(() => {
  vi.unstubAllGlobals()
  URL.createObjectURL = originalCreateObjectURL
  URL.revokeObjectURL = originalRevokeObjectURL
})

describe('SegmentDetail audio errors (D4)', () => {
  it('shows an error instead of the player when the audio fetch returns an HTTP error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        blob: () => Promise.resolve(new Blob(['{"error":"not_found"}'])),
      }),
    )

    renderDetail(makeSegment())

    await waitFor(() =>
      expect(
        screen.getByText(/Audio unavailable — this segment may have been re-cut/),
      ).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('waveform')).not.toBeInTheDocument()
    expect(screen.queryByTestId('audio-controls')).not.toBeInTheDocument()
  })

  it('shows the error on a network failure too', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('fetch failed')))

    renderDetail(makeSegment())

    await waitFor(() => expect(screen.getByText(/Audio unavailable/)).toBeInTheDocument())
  })

  it('renders the player normally when the fetch succeeds', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        blob: () => Promise.resolve(new Blob(['audio-bytes'])),
      }),
    )

    renderDetail(makeSegment())

    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled())
    expect(screen.getByTestId('waveform')).toBeInTheDocument()
    expect(screen.getByTestId('audio-controls')).toBeInTheDocument()
    expect(screen.queryByText(/Audio unavailable/)).not.toBeInTheDocument()
  })
})

describe('SegmentDetail cluster score (D6)', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        blob: () => Promise.resolve(new Blob(['audio-bytes'])),
      }),
    )
  })

  it('shows the cluster score when speaker_match_confidence is present', () => {
    renderDetail(makeSegment({ speaker_match_confidence: 0.42 }))
    expect(screen.getByText('Cluster score: 0.42')).toBeInTheDocument()
  })

  it('omits the line when speaker_match_confidence is null or absent', () => {
    const { unmount } = renderDetail(makeSegment({ speaker_match_confidence: null }))
    expect(screen.queryByText(/Cluster score/)).not.toBeInTheDocument()
    unmount()

    renderDetail(makeSegment())
    expect(screen.queryByText(/Cluster score/)).not.toBeInTheDocument()
  })
})

describe('SegmentDetail re-transcribe', () => {
  it('calls rerunSegmentTranscription for the segment when clicked', async () => {
    const client = await import('../../api/client')
    const spy = vi.mocked(client.rerunSegmentTranscription)
    spy.mockClear()

    renderDetail(makeSegment({ id: 'seg-42' }))
    const btn = screen.getByRole('button', { name: /re-transcribe/i })
    const { default: userEvent } = await import('@testing-library/user-event')
    await userEvent.setup().click(btn)

    expect(spy).toHaveBeenCalledWith('proj-1', 'seg-42')
  })
})
