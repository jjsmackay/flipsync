import { describe, it, expect } from 'vitest'
import {
  SEPARATION_KNOBS,
  DIARISATION_KNOBS,
  TRANSCRIPTION_KNOBS,
  CLEANUP_KNOBS,
  XTTS_KNOBS,
  TUNING_DEFAULTS,
  clampKnob,
  configValues,
  changedValues,
  type NumberKnob,
} from './tuning'

const ALL_KNOBS = [
  ...SEPARATION_KNOBS,
  ...DIARISATION_KNOBS,
  ...TRANSCRIPTION_KNOBS,
  ...CLEANUP_KNOBS,
  ...XTTS_KNOBS,
]

describe('knob metadata', () => {
  it('every knob has a default', () => {
    for (const knob of ALL_KNOBS) {
      expect(TUNING_DEFAULTS[knob.key], knob.key).toBeDefined()
    }
  })

  it('defaults sit inside each numeric knob’s bounds', () => {
    for (const knob of ALL_KNOBS) {
      if (knob.kind !== 'number') continue
      const dflt = TUNING_DEFAULTS[knob.key] as number
      expect(dflt, knob.key).toBeGreaterThanOrEqual(knob.min)
      expect(dflt, knob.key).toBeLessThanOrEqual(knob.max)
    }
  })

  it('knob keys are unique across stages', () => {
    const keys = ALL_KNOBS.map((k) => k.key)
    expect(new Set(keys).size).toBe(keys.length)
  })
})

describe('clampKnob', () => {
  const shifts = SEPARATION_KNOBS.find((k) => k.key === 'demucs_shifts') as NumberKnob

  it('clamps below min and above max', () => {
    expect(clampKnob(shifts, -3)).toBe(0)
    expect(clampKnob(shifts, 99)).toBe(10)
    expect(clampKnob(shifts, 4)).toBe(4)
  })

  it('falls back to min on NaN', () => {
    expect(clampKnob(shifts, NaN)).toBe(0)
  })
})

describe('configValues / changedValues', () => {
  it('extracts the knob subset from a config object', () => {
    const config = { ...TUNING_DEFAULTS, demucs_shifts: 2, match_threshold: 0.75 }
    const values = configValues(config, SEPARATION_KNOBS)
    expect(values).toEqual({ demucs_model: 'htdemucs_ft', demucs_shifts: 2 })
  })

  it('returns only keys that differ from the baseline', () => {
    const baseline = configValues(TUNING_DEFAULTS, CLEANUP_KNOBS)
    const edited = { ...baseline, highpass_hz: 120 }
    expect(changedValues(CLEANUP_KNOBS, edited, baseline)).toEqual({ highpass_hz: 120 })
  })

  it('returns an empty object when nothing changed', () => {
    const baseline = configValues(TUNING_DEFAULTS, XTTS_KNOBS)
    expect(changedValues(XTTS_KNOBS, { ...baseline }, baseline)).toEqual({})
  })
})
