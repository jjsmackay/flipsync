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

import { createPreview, getPreviews, getProject } from '../../api/client'

const models: Model[] = []

beforeEach(() => {
  vi.mocked(createPreview).mockReset()
  vi.mocked(createPreview).mockResolvedValue({ enqueued_job: { id: 'j1', type: 'preview' } } as never)
  vi.mocked(getPreviews).mockReset()
  vi.mocked(getPreviews).mockResolvedValue({ previews: [] })
  vi.mocked(getProject).mockReset()
})

function enterText() {
  fireEvent.change(screen.getByPlaceholderText('Text to synthesise…'), {
    target: { value: 'Hello there' },
  })
}

function clickZeroShotGenerate() {
  const generateButtons = screen.getAllByRole('button', { name: 'Generate' })
  fireEvent.click(generateButtons[0])
}

describe('PreviewPanel temperature', () => {
  it('sends the default temperature of 0.65', async () => {
    render(<PreviewPanel projectId="p1" models={models} />)
    enterText()
    clickZeroShotGenerate()
    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body.temperature).toBe(0.65)
  })

  it('sends the slider value once moved', async () => {
    render(<PreviewPanel projectId="p1" models={models} />)
    enterText()
    fireEvent.change(screen.getByLabelText('Temperature'), { target: { value: '1.2' } })
    clickZeroShotGenerate()
    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body.temperature).toBe(1.2)
  })
})
