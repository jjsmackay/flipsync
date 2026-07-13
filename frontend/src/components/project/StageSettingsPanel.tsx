import { useState } from 'react'
import type { ProjectConfig, PatchProjectRequest } from '../../types/api'
import { patchProject, ApiError } from '../../api/client'
import {
  changedValues,
  configValues,
  type Knob,
  type TuningKey,
  type TuningValue,
  type TuningValues,
} from '../../utils/tuning'
import { KnobFields } from './KnobFields'

interface StageSettingsPanelProps {
  projectId: string
  config: ProjectConfig
  /** The knobs this panel owns — the save PATCHes exactly this subset. */
  knobs: Knob[]
  /** Whether the step has already run: switches the saved copy to the re-run hint. */
  ranAlready: boolean
  /** Called after a successful save so the parent can refetch. */
  onSaved: () => void
  /** Header toggle: show the advanced-flagged knobs. Values/dirty tracking
   *  always cover the full knob list so toggling mid-edit loses nothing. */
  advanced?: boolean
}

// Collapsed-by-default disclosure holding one pipeline step's tuning knobs.
// Config changes apply on the step's NEXT run — the saved message says so when
// the step has already run, and the step row's Re-run button sits adjacent.
export function StageSettingsPanel({
  projectId,
  config,
  knobs,
  ranAlready,
  onSaved,
  advanced = false,
}: StageSettingsPanelProps) {
  const [values, setValues] = useState<TuningValues>(() => configValues(config, knobs))
  // Local baseline rather than the config prop: after a save the parent refetch
  // is async, and dirty must resolve immediately so the saved message shows.
  const [baseline, setBaseline] = useState<TuningValues>(() => configValues(config, knobs))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const dirty = Object.keys(changedValues(knobs, values, baseline)).length > 0

  function handleChange(key: TuningKey, value: TuningValue) {
    setValues((prev) => ({ ...prev, [key]: value }))
    setSaved(false)
  }

  async function handleSave() {
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      await patchProject(projectId, values as PatchProjectRequest)
      setBaseline(values)
      onSaved()
      setSaved(true)
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : err instanceof Error ? err.message : 'Save failed',
      )
    } finally {
      setSaving(false)
    }
  }

  function handleReset() {
    setValues(baseline)
    setError(null)
    setSaved(false)
  }

  return (
    <details className="group">
      <summary className="cursor-pointer select-none text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition-colors list-none flex items-center gap-1">
        <span className="inline-block transition-transform group-open:rotate-90">▸</span>
        Settings
      </summary>
      <div className="mt-3 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4 space-y-3">
        <KnobFields
          knobs={knobs.filter((k) => advanced || !k.advanced)}
          values={values}
          onChange={handleChange}
          idPrefix={`stage-${knobs[0]?.key ?? 'knobs'}`}
        />

        {error && (
          <p className="text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded px-2 py-1.5">
            {error}
          </p>
        )}
        {saved && !dirty && (
          <p className="text-xs text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded px-2 py-1.5">
            {ranAlready ? 'Saved — applies when this step re-runs.' : 'Saved.'}
          </p>
        )}

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void handleSave()}
            disabled={!dirty || saving}
            className="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {saving ? 'Saving…' : 'Save settings'}
          </button>
          <button
            type="button"
            onClick={handleReset}
            disabled={!dirty || saving}
            className="px-3 py-1.5 text-xs font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-50"
          >
            Reset
          </button>
        </div>
      </div>
    </details>
  )
}
