import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { TrainPanel } from './TrainPanel'
import { TUNING_DEFAULTS } from '../../utils/tuning'
import type { EngineInfo, ProjectDetail } from '../../types/api'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return { ...actual, createModel: vi.fn() }
})

import { createModel } from '../../api/client'

function makeProject(): ProjectDetail {
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

const XTTS: EngineInfo = { id: 'xtts', name: 'XTTS-v2', healthy: true, languages: ['en'] }
const XTTS_UNHEALTHY: EngineInfo = { ...XTTS, healthy: false }
const GPT_SOVITS: EngineInfo = { id: 'gpt_sovits', name: 'GPT-SoVITS', healthy: true, languages: ['en', 'zh'] }

function openConfirm() {
  fireEvent.click(screen.getByRole('button', { name: 'Train voice model' }))
}

beforeEach(() => {
  vi.mocked(createModel).mockReset()
  vi.mocked(createModel).mockResolvedValue({
    model: { id: 'm1' } as never,
    enqueued_jobs: [],
  } as never)
})

describe('TrainPanel engine picker visibility', () => {
  it('shows no picker for a single healthy engine (xtts-only) and sends engine: xtts', async () => {
    render(<TrainPanel project={makeProject()} models={[]} engines={[XTTS]} onStarted={vi.fn()} />)
    openConfirm()
    expect(screen.queryByText('Engine')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Start training' }))
    await waitFor(() => expect(createModel).toHaveBeenCalled())
    const [, body] = vi.mocked(createModel).mock.calls[0]
    expect(body.engine).toBe('xtts')
  })

  it('shows no picker for a single healthy engine (gpt-sovits-only) and sends engine: gpt_sovits', async () => {
    render(
      <TrainPanel
        project={makeProject()}
        models={[]}
        engines={[XTTS_UNHEALTHY, GPT_SOVITS]}
        onStarted={vi.fn()}
      />,
    )
    openConfirm()
    expect(screen.queryByText('Engine')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Start training' }))
    await waitFor(() => expect(createModel).toHaveBeenCalled())
    const [, body] = vi.mocked(createModel).mock.calls[0]
    expect(body.engine).toBe('gpt_sovits')
  })

  it('shows a picker when two engines are healthy, and posts the selected one', async () => {
    render(
      <TrainPanel project={makeProject()} models={[]} engines={[XTTS, GPT_SOVITS]} onStarted={vi.fn()} />,
    )
    openConfirm()
    expect(screen.getByText('Engine')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('GPT-SoVITS'))
    fireEvent.click(screen.getByRole('button', { name: 'Start training' }))
    await waitFor(() => expect(createModel).toHaveBeenCalled())
    const [, body] = vi.mocked(createModel).mock.calls[0]
    expect(body.engine).toBe('gpt_sovits')
  })
})

describe('TrainPanel GPT-SoVITS Advanced params', () => {
  it('shows the Advanced panel only for the gpt_sovits engine (single implicit engine)', () => {
    render(
      <TrainPanel
        project={makeProject()}
        models={[]}
        engines={[XTTS_UNHEALTHY, GPT_SOVITS]}
        onStarted={vi.fn()}
      />,
    )
    openConfirm()
    expect(screen.getByText('Advanced')).toBeInTheDocument()
    expect(screen.getByLabelText('SoVITS epochs')).toBeInTheDocument()
    expect(screen.getByLabelText('GPT epochs')).toBeInTheDocument()
    expect(screen.getByLabelText('Batch size')).toBeInTheDocument()
  })

  it('sends only the filled-in param keys; blank fields are omitted', async () => {
    render(
      <TrainPanel
        project={makeProject()}
        models={[]}
        engines={[XTTS_UNHEALTHY, GPT_SOVITS]}
        onStarted={vi.fn()}
      />,
    )
    openConfirm()
    fireEvent.change(screen.getByLabelText('SoVITS epochs'), { target: { value: '12' } })
    fireEvent.change(screen.getByLabelText('Batch size'), { target: { value: '4' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start training' }))
    await waitFor(() => expect(createModel).toHaveBeenCalled())
    const [, body] = vi.mocked(createModel).mock.calls[0]
    expect(body.params).toEqual({ sovits_epochs: 12, batch_size: 4 })
  })

  it('omits `params` entirely when every Advanced field is left blank', async () => {
    render(
      <TrainPanel
        project={makeProject()}
        models={[]}
        engines={[XTTS_UNHEALTHY, GPT_SOVITS]}
        onStarted={vi.fn()}
      />,
    )
    openConfirm()
    fireEvent.click(screen.getByRole('button', { name: 'Start training' }))
    await waitFor(() => expect(createModel).toHaveBeenCalled())
    const [, body] = vi.mocked(createModel).mock.calls[0]
    expect(body).not.toHaveProperty('params')
  })
})
