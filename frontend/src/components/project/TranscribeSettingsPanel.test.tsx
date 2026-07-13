import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TranscribeSettingsPanel } from './TranscribeSettingsPanel'
import type { ProjectConfig } from '../../types/api'
import { patchProject } from '../../api/client'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    patchProject: vi.fn(),
  }
})

const mockPatchProject = vi.mocked(patchProject)

function makeConfig(overrides: Partial<ProjectConfig> = {}): ProjectConfig {
  return {
    whisper_model: 'large-v3',
    language: null,
    match_threshold: 0.5,
    target_duration_secs: 600,
    auto_approve_enabled: true,
    auto_approve_match_threshold: 0.85,
    auto_approve_transcript_threshold: 0.9,
    whisper_batch_size: 16,
    whisper_compute_type: 'default',
    demucs_model: 'htdemucs_ft',
    align_words: false,
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  mockPatchProject.mockResolvedValue({} as never)
})

describe('TranscribeSettingsPanel', () => {
  it('offers all four separation models', () => {
    render(
      <TranscribeSettingsPanel projectId="proj-1" config={makeConfig()} onSaved={vi.fn()} />,
    )
    const select = screen.getByLabelText(/separation model/i) as HTMLSelectElement
    const values = Array.from(select.options).map((o) => o.value)
    expect(values).toEqual(['htdemucs', 'htdemucs_ft', 'mdx_extra', 'bs_roformer'])
  })

  it('renders an align word timestamps toggle reflecting config.align_words', () => {
    render(
      <TranscribeSettingsPanel
        projectId="proj-1"
        config={makeConfig({ align_words: false })}
        onSaved={vi.fn()}
      />,
    )
    const toggle = screen.getByRole('checkbox', { name: /align word timestamps/i })
    expect(toggle).not.toBeChecked()
  })

  it('PATCHes align_words when the toggle is changed and saved', async () => {
    const user = userEvent.setup()
    const onSaved = vi.fn()
    render(
      <TranscribeSettingsPanel
        projectId="proj-1"
        config={makeConfig({ align_words: false })}
        onSaved={onSaved}
      />,
    )
    const toggle = screen.getByRole('checkbox', { name: /align word timestamps/i })
    await user.click(toggle)
    const saveButton = screen.getByRole('button', { name: /save settings/i })
    await user.click(saveButton)

    expect(mockPatchProject).toHaveBeenCalledWith(
      'proj-1',
      expect.objectContaining({ align_words: true }),
    )
    expect(onSaved).toHaveBeenCalled()
  })

  it('PATCHes demucs_model when the separation model is changed and saved', async () => {
    const user = userEvent.setup()
    render(
      <TranscribeSettingsPanel projectId="proj-1" config={makeConfig()} onSaved={vi.fn()} />,
    )
    const select = screen.getByLabelText(/separation model/i)
    await user.selectOptions(select, 'bs_roformer')
    const saveButton = screen.getByRole('button', { name: /save settings/i })
    await user.click(saveButton)

    expect(mockPatchProject).toHaveBeenCalledWith(
      'proj-1',
      expect.objectContaining({ demucs_model: 'bs_roformer' }),
    )
  })
})
