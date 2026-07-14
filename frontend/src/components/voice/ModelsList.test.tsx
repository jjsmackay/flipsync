import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ModelsList } from './ModelsList'
import type { Model } from '../../types/api'

function makeModel(overrides: Partial<Model>): Model {
  return {
    id: 'model-1', project_id: 'p1', status: 'ready', engine: 'xtts', dataset_mode: 'approved',
    min_confidence: null, segment_count: 10, dataset_duration_secs: 400,
    dataset_manifest_path: 'models/m/dataset.json', checkpoint_dir: 'models/m',
    params: null, eval_loss: null, error: null,
    created_at: '2026-07-14T00:00:00Z', updated_at: '2026-07-14T00:00:00Z',
    ...overrides,
  }
}

describe('ModelsList engine badge', () => {
  it('renders an XTTS-v2 badge for an xtts model', () => {
    render(
      <ModelsList
        projectId="p1"
        models={[makeModel({ id: 'm-xtts', engine: 'xtts' })]}
        loading={false}
        error={null}
        onChanged={vi.fn()}
      />,
    )
    expect(screen.getByText('XTTS-v2')).toBeInTheDocument()
  })

  it('renders a GPT-SoVITS badge for a gpt_sovits model', () => {
    render(
      <ModelsList
        projectId="p1"
        models={[makeModel({ id: 'm-gpt', engine: 'gpt_sovits' })]}
        loading={false}
        error={null}
        onChanged={vi.fn()}
      />,
    )
    expect(screen.getByText('GPT-SoVITS')).toBeInTheDocument()
  })
})
