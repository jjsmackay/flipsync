import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { CompareSettingsModal } from './CompareSettingsModal'
import { TUNING_DEFAULTS } from '../../utils/tuning'
import type { ProjectConfig, Segment } from '../../types/api'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    getSegments: vi.fn(),
    createTuningPreview: vi.fn(),
    getTuningPreview: vi.fn(),
    patchProject: vi.fn(),
  }
})

import { getSegments, createTuningPreview, getTuningPreview, patchProject } from '../../api/client'

const config: ProjectConfig = {
  whisper_model: 'large-v2',
  language: null,
  match_threshold: 0.75,
  target_duration_secs: 1800,
  auto_approve_enabled: false,
  auto_approve_match_threshold: 0.9,
  auto_approve_transcript_threshold: 0.9,
  ...TUNING_DEFAULTS,
  target_lufs: -20,
}

function makeSegment(id: string): Segment {
  return {
    id,
    source_id: 's1',
    source_filename: 'file.mp4',
    start_secs: 0,
    end_secs: 4,
    duration_secs: 4,
    match_confidence: 0.9,
    transcript: 'hello there',
    transcript_edited: null,
    transcript_confidence: 0.9,
    status: 'pending',
    clipping_warning: false,
    flags: null,
    audio_url: `/projects/p1/segments/${id}/audio`,
  }
}

function renderModal(handlers: { onSaved?: () => void; onClose?: () => void } = {}) {
  render(
    <CompareSettingsModal
      projectId="p1"
      config={config}
      onSaved={handlers.onSaved ?? (() => {})}
      onClose={handlers.onClose ?? (() => {})}
    />,
  )
}

describe('CompareSettingsModal', () => {
  beforeEach(() => {
    vi.mocked(getSegments).mockReset()
    vi.mocked(createTuningPreview).mockReset()
    vi.mocked(getTuningPreview).mockReset()
    vi.mocked(patchProject).mockReset()
    vi.mocked(getSegments).mockResolvedValue({
      segments: [makeSegment('seg-1'), makeSegment('seg-2')],
      pagination: { page: 1, per_page: 50, total: 2, pages: 1 },
    })
    // Keep panes in the generating state unless a test overrides.
    vi.mocked(getTuningPreview).mockResolvedValue({ id: 'x', status: 'running', error: null })
    vi.mocked(patchProject).mockResolvedValue({} as never)
  })

  it('loads segments into the picker and seeds both columns from config', async () => {
    renderModal()
    await waitFor(() => expect(screen.getByRole('combobox')).not.toBeDisabled())
    expect(screen.getAllByText(/hello there/)).toHaveLength(2)
    // target_lufs is -20 in config — both columns seed from it.
    expect(screen.getByLabelText('Loudness (LUFS)', { selector: '#cmp-a-target_lufs' })).toHaveValue(-20)
    expect(screen.getByLabelText('Loudness (LUFS)', { selector: '#cmp-b-target_lufs' })).toHaveValue(-20)
  })

  it('submits one tuning preview per column with each column’s params', async () => {
    vi.mocked(createTuningPreview)
      .mockResolvedValueOnce({ enqueued_job: { id: 'job-a', type: 'tuning_preview' } })
      .mockResolvedValueOnce({ enqueued_job: { id: 'job-b', type: 'tuning_preview' } })
    renderModal()
    await waitFor(() => expect(screen.getByRole('combobox')).not.toBeDisabled())

    // Draft column tweak: highpass 80 → 120.
    fireEvent.change(screen.getByLabelText('High-pass (Hz)', { selector: '#cmp-b-highpass_hz' }), {
      target: { value: '120' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Run comparison' }))

    await waitFor(() => expect(createTuningPreview).toHaveBeenCalledTimes(2))
    expect(createTuningPreview).toHaveBeenNthCalledWith(1, 'p1', {
      stage: 'cleanup',
      params: {
        target_lufs: -20,
        highpass_hz: 80,
        do_trim_silence: true,
        silence_threshold_db: -50,
        silence_min_duration_secs: 0.1,
        silence_pad_start_secs: 0.05,
        silence_pad_end_secs: 0.2,
      },
      target: { segment_id: 'seg-1' },
    })
    expect(createTuningPreview).toHaveBeenNthCalledWith(2, 'p1', {
      stage: 'cleanup',
      params: {
        target_lufs: -20,
        highpass_hz: 120,
        do_trim_silence: true,
        silence_threshold_db: -50,
        silence_min_duration_secs: 0.1,
        silence_pad_start_secs: 0.05,
        silence_pad_end_secs: 0.2,
      },
      target: { segment_id: 'seg-1' },
    })
    expect(await screen.findAllByText('Processing…')).toHaveLength(2)
  })

  it('saves a column’s values as project settings', async () => {
    const onSaved = vi.fn()
    renderModal({ onSaved })
    await waitFor(() => expect(screen.getByRole('combobox')).not.toBeDisabled())

    fireEvent.change(screen.getByLabelText('High-pass (Hz)', { selector: '#cmp-b-highpass_hz' }), {
      target: { value: '120' },
    })
    fireEvent.click(screen.getAllByRole('button', { name: 'Save these settings' })[1])

    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(patchProject).toHaveBeenCalledWith('p1', {
      target_lufs: -20,
      highpass_hz: 120,
      do_trim_silence: true,
      silence_threshold_db: -50,
      silence_min_duration_secs: 0.1,
      silence_pad_start_secs: 0.05,
      silence_pad_end_secs: 0.2,
    })
    expect(await screen.findByText('Saved as project settings.')).toBeInTheDocument()
  })

  it('surfaces a failed preview’s error in its pane', async () => {
    vi.mocked(createTuningPreview)
      .mockResolvedValueOnce({ enqueued_job: { id: 'job-a', type: 'tuning_preview' } })
      .mockResolvedValueOnce({ enqueued_job: { id: 'job-b', type: 'tuning_preview' } })
    vi.mocked(getTuningPreview).mockImplementation((_pid, previewId) =>
      Promise.resolve(
        previewId === 'job-b'
          ? { id: previewId, status: 'failed', error: 'silent_after_trim' }
          : { id: previewId, status: 'running', error: null },
      ),
    )
    renderModal()
    await waitFor(() => expect(screen.getByRole('combobox')).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Run comparison' }))
    expect(await screen.findByText('silent_after_trim')).toBeInTheDocument()
  })

  it('closes on backdrop click', async () => {
    const onClose = vi.fn()
    renderModal({ onClose })
    await waitFor(() => expect(screen.getByRole('combobox')).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalled()
  })
})
