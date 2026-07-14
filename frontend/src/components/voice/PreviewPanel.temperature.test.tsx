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
  it('buttons are enabled on first render with prefilled text', () => {
    render(<PreviewPanel projectId="p1" models={models} />)
    const generateButtons = screen.getAllByRole('button', { name: 'Generate' })
    // Both zero-shot and fine-tuned buttons should be disabled initially
    // (no models, but zero-shot has no model requirement)
    expect(generateButtons[0]).not.toBeDisabled()
  })

  it('buttons disable when text is cleared', async () => {
    render(<PreviewPanel projectId="p1" models={models} />)
    const textarea = screen.getByPlaceholderText('Text to synthesise…')
    const generateButtons = screen.getAllByRole('button', { name: 'Generate' })

    // Start enabled with prefilled text
    expect(generateButtons[0]).not.toBeDisabled()

    // Clear the text
    fireEvent.change(textarea, { target: { value: '' } })

    // Both buttons should now be disabled
    expect(generateButtons[0]).toBeDisabled()
    expect(generateButtons[1]).toBeDisabled()
  })

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
      top_k: 50,
      top_p: 0.85,
      repetition_penalty: 10,
    })
  })

  it('sends the adjusted speed', async () => {
    render(<PreviewPanel projectId="p1" models={models} />)
    enterText()
    fireEvent.change(screen.getByLabelText('Speed'), { target: { value: '1.25' } })
    clickZeroShotGenerate()
    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body.speed).toBe(1.25)
  })

  it('hides advanced knobs unless advanced mode is on', () => {
    render(<PreviewPanel projectId="p1" models={models} />)
    expect(screen.queryByLabelText('Top-k')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Repetition penalty')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Temperature')).toBeInTheDocument()
  })

  it('sends top-k / top-p / repetition-penalty adjustments', async () => {
    render(<PreviewPanel projectId="p1" models={models} advanced />)
    enterText()
    fireEvent.change(screen.getByLabelText('Top-k'), { target: { value: '25' } })
    fireEvent.change(screen.getByLabelText('Top-p'), { target: { value: '0.6' } })
    fireEvent.change(screen.getByLabelText('Repetition penalty'), { target: { value: '15' } })
    clickZeroShotGenerate()
    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body.top_k).toBe(25)
    expect(body.top_p).toBe(0.6)
    expect(body.repetition_penalty).toBe(15)
  })
})
