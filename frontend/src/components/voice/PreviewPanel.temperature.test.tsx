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

describe('PreviewPanel sampling knobs', () => {
  it('sends default sampling params with every request', async () => {
    render(<PreviewPanel projectId="p1" models={models} />)
    enterText()
    clickZeroShotGenerate()
    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body).toMatchObject({
      speed: 1,
      repetition_penalty: 10,
      top_k: 50,
      top_p: 0.85,
    })
  })

  it('sends adjusted speed and repetition penalty', async () => {
    render(<PreviewPanel projectId="p1" models={models} />)
    enterText()
    fireEvent.change(screen.getByLabelText('Speed'), { target: { value: '1.25' } })
    fireEvent.change(screen.getByLabelText('Repetition penalty'), { target: { value: '5' } })
    clickZeroShotGenerate()
    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body.speed).toBe(1.25)
    expect(body.repetition_penalty).toBe(5)
  })

  it('sends advanced top-k / top-p adjustments', async () => {
    render(<PreviewPanel projectId="p1" models={models} />)
    enterText()
    fireEvent.change(screen.getByLabelText('Top-k'), { target: { value: '25' } })
    fireEvent.change(screen.getByLabelText('Top-p'), { target: { value: '0.6' } })
    clickZeroShotGenerate()
    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body.top_k).toBe(25)
    expect(body.top_p).toBe(0.6)
  })
})
