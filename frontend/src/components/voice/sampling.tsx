// Per-run XTTS sampling knobs, shared across both columns so A/B compares
// models, not sampling noise. Defaults mirror the orchestrator's.
// repetition_penalty stays server-default only — not a UI dial.
export interface SamplingParams {
  temperature: number
  speed: number
  top_k: number
  top_p: number
}

export const DEFAULT_SAMPLING: SamplingParams = {
  temperature: 0.65,
  speed: 1,
  top_k: 50,
  top_p: 0.85,
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
