import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { CreateProjectModal } from './CreateProjectModal'
import { TuningKey } from '../../utils/tuning'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return { ...actual, createProject: vi.fn() }
})

import { createProject } from '../../api/client'

const ALL_TUNING_KEYS: TuningKey[] = [
  'demucs_model',
  'demucs_shifts',
  'diar_min_speakers',
  'diar_max_speakers',
  'diar_min_segment_duration',
  'whisper_beam_size',
  'whisper_vad_filter',
  'whisper_batch_size',
  'whisper_compute_type',
  'target_lufs',
  'highpass_hz',
  'silence_threshold_db',
  'silence_min_duration_secs',
  'xtts_epochs',
  'xtts_batch_size',
  'xtts_grad_accum',
  'xtts_learning_rate',
]

function fillNameAndSubmit() {
  fireEvent.change(screen.getByPlaceholderText('e.g. My Speaker Dataset'), {
    target: { value: 'My Project' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Create project' }))
}

beforeEach(() => {
  vi.mocked(createProject).mockReset()
  vi.mocked(createProject).mockResolvedValue({ id: 'p1', name: 'x', status: 'new' })
})

describe('CreateProjectModal advanced knobs', () => {
  it('omits all tuning keys when Advanced is left untouched', async () => {
    render(<CreateProjectModal onCreated={vi.fn()} onClose={vi.fn()} />)
    fillNameAndSubmit()
    await waitFor(() => expect(createProject).toHaveBeenCalled())
    const [body] = vi.mocked(createProject).mock.calls[0]
    for (const key of ALL_TUNING_KEYS) {
      expect(key in body).toBe(false)
    }
  })

  it('sends only the changed tuning fields when Advanced is edited', async () => {
    render(<CreateProjectModal onCreated={vi.fn()} onClose={vi.fn()} />)
    fireEvent.click(screen.getByText('Advanced'))
    fireEvent.change(screen.getByLabelText('Shifts'), { target: { value: '2' } })
    fireEvent.click(screen.getByLabelText('VAD filter'))
    fillNameAndSubmit()
    await waitFor(() => expect(createProject).toHaveBeenCalled())
    const [body] = vi.mocked(createProject).mock.calls[0]
    const bodyRecord = body as unknown as Record<string, unknown>
    const tuningKeysPresent = ALL_TUNING_KEYS.filter((k) => k in bodyRecord)
    expect(tuningKeysPresent.sort()).toEqual(['demucs_shifts', 'whisper_vad_filter'])
    expect(body.demucs_shifts).toBe(2)
    expect(body.whisper_vad_filter).toBe(true)
  })
})
