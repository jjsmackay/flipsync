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
  | 'do_trim_silence'
  | 'silence_threshold_db'
  | 'silence_min_duration_secs'
  | 'silence_pad_start_secs'
  | 'silence_pad_end_secs'
  | 'xtts_epochs'
  | 'xtts_batch_size'
  | 'xtts_grad_accum'
  | 'xtts_learning_rate'

interface KnobBase {
  key: TuningKey
  label: string
  hint?: string
  /** Hidden unless the header's Advanced toggle is on. Basic = you'd touch it
   *  because you heard a problem; advanced = GPU limits, sampling internals, DSP. */
  advanced?: boolean
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
    hint: 'htdemucs_ft is the fine-tuned default. bs_roformer separates best but runs slower and downloads its own weights. mdx_extra is the fallback.',
  },
  {
    kind: 'number',
    key: 'demucs_shifts',
    advanced: true,
    label: 'Shifts',
    min: 0,
    max: 10,
    step: 1,
    hint: 'Runs separation again on time-shifted copies and averages them. Cleaner vocals at N+1 times the runtime.',
  },
]

export const DIARISATION_KNOBS: Knob[] = [
  {
    kind: 'number',
    key: 'diar_min_speakers',
    label: 'Min speakers',
    min: 1,
    max: 20,
    step: 1,
    hint: 'The fewest voices diarisation may find. Raise it if two people keep merging into one.',
  },
  {
    kind: 'number',
    key: 'diar_max_speakers',
    label: 'Max speakers',
    min: 1,
    max: 20,
    step: 1,
    hint: 'The most voices diarisation may find. Lower it if one person keeps splitting in two.',
  },
  {
    kind: 'number',
    key: 'diar_min_segment_duration',
    advanced: true,
    label: 'Min segment (s)',
    min: 0.1,
    max: 30,
    step: 0.1,
    hint: 'Drops speaker turns shorter than this. Raise it to skip grunts and one-word replies.',
  },
]

export const TRANSCRIPTION_KNOBS: Knob[] = [
  {
    kind: 'number',
    key: 'whisper_batch_size',
    advanced: true,
    label: 'Batch size',
    min: 1,
    max: 64,
    step: 1,
    hint: 'How many segments transcribe at once. Reduce it if the GPU runs out of memory.',
  },
  {
    kind: 'select',
    key: 'whisper_compute_type',
    advanced: true,
    label: 'Precision',
    options: WHISPER_COMPUTE_TYPES,
    hint: 'Lighter precisions like int8_float16 cut VRAM use on a constrained GPU.',
  },
  {
    kind: 'number',
    key: 'whisper_beam_size',
    advanced: true,
    label: 'Beam size',
    min: 1,
    max: 10,
    step: 1,
    hint: 'How many transcript candidates whisper weighs per step. Wider beams gain accuracy and cost time.',
  },
  {
    kind: 'checkbox',
    key: 'whisper_vad_filter',
    label: 'VAD filter',
    hint: 'Drops non-speech before decoding. Turn it on if transcripts hallucinate over music or silence.',
  },
  {
    kind: 'checkbox',
    key: 'align_words',
    label: 'Align words',
    hint: 'Refines word timestamps with a forced-alignment pass before sentence re-segmentation. Transcripts stay untouched.',
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
    hint: 'Normalisation target. −23 is broadcast standard; −19 comes out louder.',
  },
  {
    kind: 'number',
    key: 'highpass_hz',
    advanced: true,
    label: 'High-pass (Hz)',
    min: 0,
    max: 1000,
    step: 5,
    hint: 'Cuts rumble below this frequency. 0 disables the filter.',
  },
  {
    kind: 'checkbox',
    key: 'do_trim_silence',
    label: 'Trim silence',
    hint: 'Trim leading/trailing silence from each segment. Turn off to keep the diariser boundaries when trimming eats speech onsets.',
  },
  {
    kind: 'number',
    key: 'silence_threshold_db',
    advanced: true,
    label: 'Silence threshold (dB)',
    min: -90,
    max: 0,
    step: 1,
    hint: 'Audio quieter than this counts as silence when trimming segment edges.',
  },
  {
    kind: 'number',
    key: 'silence_min_duration_secs',
    advanced: true,
    label: 'Silence min (s)',
    min: 0,
    max: 10,
    step: 0.05,
    hint: 'Silence must last this long before trimming touches it. Shorter gaps survive.',
  },
  {
    kind: 'number',
    key: 'silence_pad_start_secs',
    label: 'Head pad (s)',
    min: 0,
    max: 2,
    step: 0.05,
    hint: 'Silence re-added to the start after trimming, so the clip has a clean attack instead of a hard cut. 0 disables.',
  },
  {
    kind: 'number',
    key: 'silence_pad_end_secs',
    label: 'Tail pad (s)',
    min: 0,
    max: 2,
    step: 0.05,
    hint: 'Silence re-added to the end after trimming, so the clip has a clean decay instead of a hard cut. 0 disables.',
  },
]

export const XTTS_KNOBS: Knob[] = [
  {
    kind: 'number',
    key: 'xtts_epochs',
    label: 'Epochs',
    min: 1,
    max: 200,
    step: 1,
    hint: 'Full passes over the dataset. More epochs fit the voice tighter; too many overfit a small dataset.',
  },
  {
    kind: 'number',
    key: 'xtts_batch_size',
    advanced: true,
    label: 'Batch size',
    min: 1,
    max: 64,
    step: 1,
    hint: 'Samples per training step. Reduce it if fine-tuning runs out of GPU memory.',
  },
  {
    kind: 'number',
    key: 'xtts_grad_accum',
    advanced: true,
    label: 'Grad accum',
    min: 1,
    max: 64,
    step: 1,
    hint: 'Accumulates gradients across this many steps to mimic a larger batch without the VRAM cost.',
  },
  {
    kind: 'number',
    key: 'xtts_learning_rate',
    advanced: true,
    label: 'Learning rate',
    min: 1e-7,
    max: 1,
    step: 1e-6,
    hint: 'How far each step moves the model. Lower is safer; higher trains faster and can destabilise.',
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
  do_trim_silence: true,
  silence_threshold_db: -50,
  silence_min_duration_secs: 0.1,
  silence_pad_start_secs: 0.05,
  silence_pad_end_secs: 0.2,
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
