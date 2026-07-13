import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { KnobFields } from './KnobFields'
import { SEPARATION_KNOBS, TRANSCRIPTION_KNOBS, TUNING_DEFAULTS, configValues } from '../../utils/tuning'

describe('KnobFields', () => {
  it('renders a field per knob with the given values', () => {
    const values = { ...configValues(TUNING_DEFAULTS, SEPARATION_KNOBS), demucs_shifts: 3 }
    render(
      <KnobFields knobs={SEPARATION_KNOBS} values={values} onChange={() => {}} idPrefix="t" />,
    )
    expect(screen.getByLabelText('Separation model')).toHaveValue('htdemucs_ft')
    expect(screen.getByLabelText('Shifts')).toHaveValue(3)
  })

  it('emits select changes', () => {
    const onChange = vi.fn()
    render(
      <KnobFields
        knobs={SEPARATION_KNOBS}
        values={configValues(TUNING_DEFAULTS, SEPARATION_KNOBS)}
        onChange={onChange}
        idPrefix="t"
      />,
    )
    fireEvent.change(screen.getByLabelText('Separation model'), { target: { value: 'mdx_extra' } })
    expect(onChange).toHaveBeenCalledWith('demucs_model', 'mdx_extra')
  })

  it('emits checkbox toggles', () => {
    const onChange = vi.fn()
    render(
      <KnobFields
        knobs={TRANSCRIPTION_KNOBS}
        values={configValues(TUNING_DEFAULTS, TRANSCRIPTION_KNOBS)}
        onChange={onChange}
        idPrefix="t"
      />,
    )
    fireEvent.click(screen.getByLabelText('VAD filter'))
    expect(onChange).toHaveBeenCalledWith('whisper_vad_filter', true)
  })

  it('clamps numeric input to the knob bounds on commit', () => {
    const onChange = vi.fn()
    render(
      <KnobFields
        knobs={SEPARATION_KNOBS}
        values={configValues(TUNING_DEFAULTS, SEPARATION_KNOBS)}
        onChange={onChange}
        idPrefix="t"
      />,
    )
    const shifts = screen.getByLabelText('Shifts')
    fireEvent.change(shifts, { target: { value: '99' } })
    expect(onChange).toHaveBeenCalledWith('demucs_shifts', 10)
  })

  it('does not emit for unparseable numeric input, and restores on blur', () => {
    const onChange = vi.fn()
    render(
      <KnobFields
        knobs={SEPARATION_KNOBS}
        values={configValues(TUNING_DEFAULTS, SEPARATION_KNOBS)}
        onChange={onChange}
        idPrefix="t"
      />,
    )
    const shifts = screen.getByLabelText('Shifts')
    fireEvent.change(shifts, { target: { value: '' } })
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.blur(shifts)
    expect(shifts).toHaveValue(0)
  })

  it('disables every field when disabled', () => {
    render(
      <KnobFields
        knobs={SEPARATION_KNOBS}
        values={configValues(TUNING_DEFAULTS, SEPARATION_KNOBS)}
        onChange={() => {}}
        idPrefix="t"
        disabled
      />,
    )
    expect(screen.getByLabelText('Separation model')).toBeDisabled()
    expect(screen.getByLabelText('Shifts')).toBeDisabled()
  })
})
