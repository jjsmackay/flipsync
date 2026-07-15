import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { PreviewPanel } from './PreviewPanel'
import type { Model } from '../../types/api'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    createPreview: vi.fn(),
    getPreviews: vi.fn(),
    getProject: vi.fn(),
  }
})

import { createPreview, getPreviews } from '../../api/client'

const readyGptSovitsModel: Model = {
  id: 'model-1', project_id: 'p1', status: 'ready', engine: 'gpt_sovits', dataset_mode: 'approved',
  min_confidence: null, segment_count: 10, dataset_duration_secs: 400,
  dataset_manifest_path: 'models/m/dataset.json', checkpoint_dir: 'models/m',
  params: null, eval_loss: null, error: null,
  created_at: '2026-07-14T00:00:00Z', updated_at: '2026-07-14T00:00:00Z',
}

beforeEach(() => {
  vi.mocked(createPreview).mockReset()
  vi.mocked(getPreviews).mockReset().mockResolvedValue({ previews: [] })
})

describe('PreviewPanel base-model gating', () => {
  it('shows the zero-shot base-model column by default (xtts-available deployments)', () => {
    render(<PreviewPanel projectId="p1" models={[]} />)
    expect(screen.getByText('Zero-shot (base model)')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Generate' })).toHaveLength(2)
  })

  it('hides the zero-shot base-model column for a gpt-sovits-only deployment', () => {
    render(<PreviewPanel projectId="p1" models={[readyGptSovitsModel]} xttsAvailable={false} />)
    expect(screen.queryByText('Zero-shot (base model)')).toBeNull()
    // Only the fine-tuned column's Generate button remains.
    expect(screen.getAllByRole('button', { name: 'Generate' })).toHaveLength(1)
  })

  it('still requires a ready model for the fine-tuned column when the base column is hidden', () => {
    render(<PreviewPanel projectId="p1" models={[]} xttsAvailable={false} />)
    expect(screen.getByRole('button', { name: 'Generate' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }))
    expect(createPreview).not.toHaveBeenCalled()
  })
})

describe('PreviewPanel per-engine sampling', () => {
  const samplingKeys = ['temperature', 'speed', 'repetition_penalty', 'top_k', 'top_p',
                        'length_penalty', 'num_beams', 'enable_text_splitting'] as const

  beforeEach(() => {
    vi.mocked(createPreview).mockResolvedValue({ enqueued_job: { id: 'j1', type: 'preview' } })
  })

  it('omits every sampling knob for a gpt_sovits fine-tuned preview', async () => {
    render(<PreviewPanel projectId="p1" models={[readyGptSovitsModel]} xttsAvailable={false} />)
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }))
    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body.model_id).toBe('model-1')
    for (const key of samplingKeys) {
      expect(body).not.toHaveProperty(key)
    }
  })

  it('still sends XTTS sampling values for an xtts fine-tuned preview', async () => {
    const xttsModel: Model = { ...readyGptSovitsModel, id: 'model-x', engine: 'xtts' }
    render(<PreviewPanel projectId="p1" models={[xttsModel]} />)
    const generateButtons = screen.getAllByRole('button', { name: 'Generate' })
    fireEvent.click(generateButtons[1]) // fine-tuned column
    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body).toMatchObject({ model_id: 'model-x', temperature: 0.65, speed: 1, top_k: 50, top_p: 0.85 })
  })

  it('still sends XTTS sampling for the base column when a gpt_sovits model is selected', async () => {
    render(<PreviewPanel projectId="p1" models={[readyGptSovitsModel]} />)
    const generateButtons = screen.getAllByRole('button', { name: 'Generate' })
    fireEvent.click(generateButtons[0]) // zero-shot base column
    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body).toMatchObject({ model_id: null, temperature: 0.65 })
  })

  it('hides the sampling sliders when only a gpt_sovits model can be previewed', () => {
    render(<PreviewPanel projectId="p1" models={[readyGptSovitsModel]} xttsAvailable={false} />)
    expect(screen.queryByLabelText('Temperature')).toBeNull()
    expect(screen.queryByLabelText('Speed')).toBeNull()
  })

  it('keeps the sliders (they drive the base column) in a mixed deployment', () => {
    render(<PreviewPanel projectId="p1" models={[readyGptSovitsModel]} />)
    expect(screen.getByLabelText('Temperature')).toBeInTheDocument()
  })
})
