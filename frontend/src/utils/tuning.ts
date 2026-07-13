import { DEMUCS_MODELS, WHISPER_COMPUTE_TYPES } from '../types/api'

// Single source of truth for the pipeline tuning knobs the UI exposes: which
// keys exist, their labels/hints, and their client-side bounds (mirroring the
// server validators in the orchestrator — the server 422 remains the backstop).
// Panels, the compare modal, and the create-project form all render from these
// arrays via KnobFields, so adding a knob is a one-entry change here.

export type TuningKey =
  | 'demucs_model'
  | 'demucs_shifts'
  | 'diar_min_speakers'
  | 'diar_max_speakers'
  | 'diar_min_segment_duration'
  | 'whisper_beam_size'
  | 'whisper_vad_filter'
  | 'align_words'
  | 'whisper_batch_size'
  | 'whisper_compute_type'
  | 'target_lufs'
  | 'highpass_hz'
  | 'silence_threshold_db'
  | 'silence_min_duration_secs'
  | 'xtts_epochs'
  | 'xtts_batch_size'
  | 'xtts_grad_accum'
  | 'xtts_learning_rate'

interface KnobBase {
  key: TuningKey
  label: string
  hint?: string
}
export interface NumberKnob extends KnobBase {
  kind: 'number'
  min: number
  max: number
  step: number
}
export interface SelectKnob extends KnobBase {
  kind: 'select'
  options: readonly string[]
}
export interface CheckboxKnob extends KnobBase {
  kind: 'checkbox'
}
export type Knob = NumberKnob | SelectKnob | CheckboxKnob

export type TuningValue = number | string | boolean
export type TuningValues = Partial<Record<TuningKey, TuningValue>>

export const SEPARATION_KNOBS: Knob[] = [
  {
    kind: 'select',
    key: 'demucs_model',
    label: 'Separation model',
    options: DEMUCS_MODELS,
    hint: 'htdemucs_ft is the fine-tuned default; bs_roformer is the strongest (slower, separate weights); mdx_extra is the fallback.',
  },
  {
    kind: 'number',
    key: 'demucs_shifts',
    label: 'Shifts',
    min: 0,
    max: 10,
    step: 1,
    hint: 'Extra augmentation passes — cleaner separation at N+1× the runtime.',
  },
]

export const DIARISATION_KNOBS: Knob[] = [
  { kind: 'number', key: 'diar_min_speakers', label: 'Min speakers', min: 1, max: 20, step: 1 },
  { kind: 'number', key: 'diar_max_speakers', label: 'Max speakers', min: 1, max: 20, step: 1 },
  {
    kind: 'number',
    key: 'diar_min_segment_duration',
    label: 'Min segment (s)',
    min: 0.1,
    max: 30,
    step: 0.1,
    hint: 'Speaker turns shorter than this are dropped.',
  },
]

export const TRANSCRIPTION_KNOBS: Knob[] = [
  {
    kind: 'number',
    key: 'whisper_batch_size',
    label: 'Batch size',
    min: 1,
    max: 64,
    step: 1,
    hint: 'Segments transcribed concurrently — reduce if the GPU runs out of memory.',
  },
  {
    kind: 'select',
    key: 'whisper_compute_type',
    label: 'Precision',
    options: WHISPER_COMPUTE_TYPES,
    hint: 'A lighter precision (e.g. int8_float16) cuts VRAM on a constrained GPU.',
  },
  {
    kind: 'number',
    key: 'whisper_beam_size',
    label: 'Beam size',
    min: 1,
    max: 10,
    step: 1,
    hint: 'Wider beams are slightly more accurate and slower.',
  },
  {
    kind: 'checkbox',
    key: 'whisper_vad_filter',
    label: 'VAD filter',
    hint: 'Drop non-speech before decoding — helps if transcripts hallucinate on music or silence.',
  },
  {
    kind: 'checkbox',
    key: 'align_words',
    label: 'Align words',
    hint: 'Forced-alignment pass that refines word timestamps before sentence re-segmentation. Timestamps only — transcripts unaffected.',
  },
]

export const CLEANUP_KNOBS: Knob[] = [
  {
    kind: 'number',
    key: 'target_lufs',
    label: 'Loudness (LUFS)',
    min: -70,
    max: -5,
    step: 0.5,
    hint: 'Normalisation target. −23 is broadcast standard; higher (e.g. −19) is louder.',
  },
  {
    kind: 'number',
    key: 'highpass_hz',
    label: 'High-pass (Hz)',
    min: 0,
    max: 1000,
    step: 5,
    hint: 'Cuts rumble below this frequency. 0 disables.',
  },
  {
    kind: 'number',
    key: 'silence_threshold_db',
    label: 'Silence threshold (dB)',
    min: -90,
    max: 0,
    step: 1,
    hint: 'Audio quieter than this counts as silence for edge trimming.',
  },
  {
    kind: 'number',
    key: 'silence_min_duration_secs',
    label: 'Silence min (s)',
    min: 0,
    max: 10,
    step: 0.05,
    hint: 'Silence shorter than this is kept.',
  },
]

export const XTTS_KNOBS: Knob[] = [
  { kind: 'number', key: 'xtts_epochs', label: 'Epochs', min: 1, max: 200, step: 1 },
  {
    kind: 'number',
    key: 'xtts_batch_size',
    label: 'Batch size',
    min: 1,
    max: 64,
    step: 1,
    hint: 'Reduce if fine-tuning runs out of GPU memory.',
  },
  { kind: 'number', key: 'xtts_grad_accum', label: 'Grad accum', min: 1, max: 64, step: 1 },
  {
    kind: 'number',
    key: 'xtts_learning_rate',
    label: 'Learning rate',
    min: 1e-7,
    max: 1,
    step: 1e-6,
  },
]

// Server-side defaults (migration 011 + earlier), used to seed the create-project
// form and to spread into test factories. Must match the orchestrator's column
// defaults exactly.
export const TUNING_DEFAULTS = {
  demucs_model: 'htdemucs_ft',
  demucs_shifts: 0,
  diar_min_speakers: 1,
  diar_max_speakers: 10,
  diar_min_segment_duration: 1.0,
  whisper_beam_size: 5,
  whisper_vad_filter: false,
  align_words: false,
  whisper_batch_size: 16,
  whisper_compute_type: 'default',
  target_lufs: -23,
  highpass_hz: 80,
  silence_threshold_db: -50,
  silence_min_duration_secs: 0.1,
  xtts_epochs: 10,
  xtts_batch_size: 3,
  xtts_grad_accum: 1,
  xtts_learning_rate: 5e-6,
} satisfies Record<TuningKey, TuningValue>

/** Clamp a raw numeric input to the knob's bounds; NaN falls back to min. */
export function clampKnob(knob: NumberKnob, raw: number): number {
  if (Number.isNaN(raw)) return knob.min
  return Math.min(knob.max, Math.max(knob.min, raw))
}

/** Extract the values for a knob list from a config-shaped object
 *  (ProjectConfig or any partial knob map). */
export function configValues(
  config: Partial<Record<TuningKey, TuningValue>>,
  knobs: Knob[],
): TuningValues {
  const out: TuningValues = {}
  for (const knob of knobs) {
    out[knob.key] = config[knob.key]
  }
  return out
}

/** The subset of `values` that differs from `baseline` — what a save should PATCH. */
export function changedValues(
  knobs: Knob[],
  values: TuningValues,
  baseline: TuningValues,
): TuningValues {
  const out: TuningValues = {}
  for (const knob of knobs) {
    if (values[knob.key] !== baseline[knob.key]) {
      out[knob.key] = values[knob.key]
    }
  }
  return out
}
