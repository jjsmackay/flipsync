import { useEffect, useState } from 'react'
import {
  clampKnob,
  type Knob,
  type NumberKnob,
  type TuningKey,
  type TuningValue,
  type TuningValues,
} from '../../utils/tuning'

interface KnobFieldsProps {
  knobs: Knob[]
  values: TuningValues
  onChange: (key: TuningKey, value: TuningValue) => void
  disabled?: boolean
  /** Unique per mount so field ids don't collide (the compare modal renders two columns). */
  idPrefix: string
}

const INPUT_CLASS =
  'border border-gray-300 dark:border-gray-600 rounded px-2 py-1 text-sm dark:bg-gray-900 dark:text-gray-100 disabled:opacity-50'
const LABEL_CLASS = 'block text-xs font-medium text-gray-600 dark:text-gray-400 mb-0.5'

/** Numeric knob input: free typing in local state, committed (clamped) on every
 *  parseable change so dirty-tracking stays live; blur snaps the display back to
 *  the committed value. Keeps clamping from fighting mid-edit keystrokes like "-". */
function NumberField({
  knob,
  value,
  onCommit,
  disabled,
  id,
}: {
  knob: NumberKnob
  value: number
  onCommit: (value: number) => void
  disabled?: boolean
  id: string
}) {
  const [text, setText] = useState(String(value))

  // External value changed (reset, config refetch) — resync the display.
  useEffect(() => {
    setText(String(value))
  }, [value])

  return (
    <input
      id={id}
      type="number"
      min={knob.min}
      max={knob.max}
      step={knob.step}
      value={text}
      disabled={disabled}
      onChange={(e) => {
        setText(e.target.value)
        const parsed = parseFloat(e.target.value)
        if (!Number.isNaN(parsed)) onCommit(clampKnob(knob, parsed))
      }}
      onBlur={() => setText(String(value))}
      className={`${INPUT_CLASS} w-[120px]`}
    />
  )
}

const HINT_CLASS = 'flex-1 min-w-0 text-xs text-gray-500 dark:text-gray-400'

// One knob per row: the field stacked on the left (fixed width), its help text
// running beside it. Hints are always visible — no hover-only tooltips.
export function KnobFields({ knobs, values, onChange, disabled, idPrefix }: KnobFieldsProps) {
  return (
    <div className="space-y-3">
      {knobs.map((knob) => {
        const id = `${idPrefix}-${knob.key}`
        if (knob.kind === 'checkbox') {
          return (
            <div key={knob.key} className="flex items-start gap-4">
              <label
                htmlFor={id}
                className="flex w-44 shrink-0 items-center gap-2 text-xs font-medium text-gray-600 dark:text-gray-400 cursor-pointer select-none"
              >
                <input
                  id={id}
                  type="checkbox"
                  checked={Boolean(values[knob.key])}
                  disabled={disabled}
                  onChange={(e) => onChange(knob.key, e.target.checked)}
                  className="accent-blue-600 w-4 h-4 disabled:opacity-50"
                />
                {knob.label}
              </label>
              {knob.hint && <p className={HINT_CLASS}>{knob.hint}</p>}
            </div>
          )
        }
        return (
          <div key={knob.key} className="flex items-start gap-4">
            <div className="w-44 shrink-0">
              <label htmlFor={id} className={LABEL_CLASS}>
                {knob.label}
              </label>
              {knob.kind === 'select' ? (
                <select
                  id={id}
                  value={String(values[knob.key] ?? '')}
                  disabled={disabled}
                  onChange={(e) => onChange(knob.key, e.target.value)}
                  className={`${INPUT_CLASS} w-[120px]`}
                >
                  {knob.options.map((opt) => (
                    <option key={opt} value={opt}>
                      {opt}
                    </option>
                  ))}
                </select>
              ) : (
                <NumberField
                  knob={knob}
                  id={id}
                  value={Number(values[knob.key] ?? knob.min)}
                  disabled={disabled}
                  onCommit={(v) => onChange(knob.key, v)}
                />
              )}
            </div>
            {/* pt aligns the first hint line with the input, past the stacked label */}
            {knob.hint && <p className={`${HINT_CLASS} pt-5`}>{knob.hint}</p>}
          </div>
        )
      })}
    </div>
  )
}
