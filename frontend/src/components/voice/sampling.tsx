// Per-run XTTS sampling knobs, shared across both columns so A/B compares
// models, not sampling noise. Defaults mirror the orchestrator's.
export interface SamplingParams {
  temperature: number
  speed: number
  top_k: number
  top_p: number
  repetition_penalty: number
  length_penalty: number
  num_beams: number
  enable_text_splitting: boolean
}

/** Keys of SamplingParams that a numeric SliderRow drives (i.e. all but the
 *  boolean enable_text_splitting, which gets a CheckboxRow). */
export type NumericSamplingKey = Exclude<keyof SamplingParams, 'enable_text_splitting'>

export const DEFAULT_SAMPLING: SamplingParams = {
  temperature: 0.65,
  speed: 1,
  top_k: 50,
  top_p: 0.85,
  repetition_penalty: 10,
  length_penalty: 1,
  num_beams: 1,
  enable_text_splitting: true,
}

export function SliderRow({
  id,
  label,
  min,
  max,
  step,
  value,
  decimals,
  hint,
  onChange,
}: {
  id: string
  label: string
  min: number
  max: number
  step: number
  value: number
  decimals: number
  hint: string
  onChange: (value: number) => void
}) {
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-3">
        <label htmlFor={id} className="text-sm text-gray-700 dark:text-gray-300">
          {label}
        </label>
        <span className="shrink-0 font-mono text-blue-600">{value.toFixed(decimals)}</span>
      </div>
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="w-full accent-blue-600"
      />
      <p className="text-xs text-gray-500 dark:text-gray-400">{hint}</p>
    </div>
  )
}

export function CheckboxRow({
  id,
  label,
  checked,
  hint,
  onChange,
}: {
  id: string
  label: string
  checked: boolean
  hint: string
  onChange: (checked: boolean) => void
}) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
        <input
          id={id}
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="accent-blue-600"
        />
        {label}
      </label>
      <p className="text-xs text-gray-500 dark:text-gray-400">{hint}</p>
    </div>
  )
}
