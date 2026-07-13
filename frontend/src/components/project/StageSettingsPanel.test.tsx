import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { StageSettingsPanel } from './StageSettingsPanel'
import { SEPARATION_KNOBS, TUNING_DEFAULTS } from '../../utils/tuning'
import type { ProjectConfig } from '../../types/api'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return { ...actual, patchProject: vi.fn() }
})

import { patchProject } from '../../api/client'

const config: ProjectConfig = {
  whisper_model: 'large-v2',
  language: null,
  match_threshold: 0.75,
  target_duration_secs: 1800,
  auto_approve_enabled: false,
  auto_approve_match_threshold: 0.9,
  auto_approve_transcript_threshold: 0.9,
  ...TUNING_DEFAULTS,
}

function renderPanel(
  overrides: { ranAlready?: boolean; onSaved?: () => void; advanced?: boolean } = {},
) {
  render(
    <StageSettingsPanel
      projectId="p1"
      config={config}
      knobs={SEPARATION_KNOBS}
      ranAlready={overrides.ranAlready ?? false}
      onSaved={overrides.onSaved ?? (() => {})}
      // Most tests drive Shifts, an advanced knob — show them by default.
      advanced={overrides.advanced ?? true}
    />,
  )
  // The disclosure starts collapsed — open it.
  fireEvent.click(screen.getByText('Settings'))
}

describe('StageSettingsPanel', () => {
  beforeEach(() => {
    vi.mocked(patchProject).mockReset()
    vi.mocked(patchProject).mockResolvedValue({} as never)
  })

  it('renders the config values and disables Save until dirty', () => {
    renderPanel()
    expect(screen.getByLabelText('Shifts')).toHaveValue(0)
    expect(screen.getByRole('button', { name: 'Save settings' })).toBeDisabled()
  })

  it('hides advanced knobs unless the toggle is on', () => {
    renderPanel({ advanced: false })
    expect(screen.getByLabelText('Separation model')).toBeInTheDocument()
    expect(screen.queryByLabelText('Shifts')).not.toBeInTheDocument()
  })

  it('saves only this panel’s knob subset', async () => {
    const onSaved = vi.fn()
    renderPanel({ onSaved })
    fireEvent.change(screen.getByLabelText('Shifts'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(patchProject).toHaveBeenCalledWith('p1', {
      demucs_model: 'htdemucs_ft',
      demucs_shifts: 2,
    })
  })

  it('shows the re-run hint after saving when the step already ran', async () => {
    renderPanel({ ranAlready: true })
    fireEvent.change(screen.getByLabelText('Shifts'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))
    expect(
      await screen.findByText('Saved — applies when this step re-runs.'),
    ).toBeInTheDocument()
  })

  it('shows a plain saved message when the step has not run yet', async () => {
    renderPanel({ ranAlready: false })
    fireEvent.change(screen.getByLabelText('Shifts'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
  })

  it('Reset restores the config values', () => {
    renderPanel()
    fireEvent.change(screen.getByLabelText('Shifts'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
    expect(screen.getByLabelText('Shifts')).toHaveValue(0)
    expect(screen.getByRole('button', { name: 'Save settings' })).toBeDisabled()
  })

  it('surfaces API errors', async () => {
    vi.mocked(patchProject).mockRejectedValue(new Error('nope'))
    renderPanel()
    fireEvent.change(screen.getByLabelText('Shifts'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }))
    expect(await screen.findByText('nope')).toBeInTheDocument()
  })
})
