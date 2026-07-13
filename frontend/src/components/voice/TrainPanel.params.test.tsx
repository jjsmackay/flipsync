import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { TrainPanel } from './TrainPanel'
import { TUNING_DEFAULTS } from '../../utils/tuning'
import type { ProjectDetail } from '../../types/api'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return { ...actual, createModel: vi.fn() }
})

import { createModel } from '../../api/client'

function makeProject(overrides: { xttsEpochs?: number } = {}): ProjectDetail {
  return {
    id: 'p1',
    name: 'Test',
    status: 'review',
    created_at: '',
    updated_at: '',
    reference_path: '/data/ref.wav',
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
      xtts_epochs: overrides.xttsEpochs ?? TUNING_DEFAULTS.xtts_epochs,
    },
    active_jobs: [],
    recent_failed_jobs: [],
    stats: {
      approved_count: 100,
      approved_duration_secs: 1800,
      pending_count: 0,
      maybe_count: 0,
      total_segments: 100,
      auto_approved_count: 0,
      rejected_count: 0,
      below_threshold_count: 0,
      source_coverage: [],
    },
  }
}

function openConfirm() {
  fireEvent.click(screen.getByRole('button', { name: 'Train voice model' }))
}

// The Train panel no longer carries per-run hyperparameter overrides: epochs,
// batch size, etc. are set once in the Train step's persisted Settings
// (xtts_* project config) and the orchestrator reads them for every run. The
// train request must therefore never send `params`, which would otherwise
// clobber the saved settings.
describe('TrainPanel train request', () => {
  beforeEach(() => {
    vi.mocked(createModel).mockReset()
    vi.mocked(createModel).mockResolvedValue({
      model: { id: 'm1' } as never,
      enqueued_jobs: [],
    } as never)
  })

  it('never sends per-run params (training settings drive the run)', async () => {
    render(<TrainPanel project={makeProject()} models={[]} onStarted={vi.fn()} />)
    openConfirm()
    fireEvent.click(screen.getByRole('button', { name: 'Start training' }))
    await waitFor(() => expect(createModel).toHaveBeenCalled())
    const [, body] = vi.mocked(createModel).mock.calls[0]
    expect(body).not.toHaveProperty('params')
  })

  it('sends no params even when the project has a non-default epochs setting', async () => {
    render(<TrainPanel project={makeProject({ xttsEpochs: 42 })} models={[]} onStarted={vi.fn()} />)
    openConfirm()
    fireEvent.click(screen.getByRole('button', { name: 'Start training' }))
    await waitFor(() => expect(createModel).toHaveBeenCalled())
    const [, body] = vi.mocked(createModel).mock.calls[0]
    expect(body).not.toHaveProperty('params')
  })

  it('exposes no Advanced hyperparameter disclosure', () => {
    render(<TrainPanel project={makeProject()} models={[]} onStarted={vi.fn()} />)
    openConfirm()
    expect(screen.queryByText('Advanced')).toBeNull()
    expect(screen.queryByLabelText('Epochs')).toBeNull()
  })
})
