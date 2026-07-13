import { useState } from 'react'
import type { ProjectConfig } from '../../types/api'
import { WHISPER_COMPUTE_TYPES } from '../../types/api'
import { patchProject, ApiError } from '../../api/client'

interface TranscribeSettingsPanelProps {
  projectId: string
  config: ProjectConfig
  /** Called after a successful save so the parent can refetch. */
  onSaved: () => void
}

// Whisper transcription tuning — GPU/VRAM levers, applied when transcription
// runs (or re-runs) during review. Kept separate from the review thresholds so
// each saves its own subset of the project config.
export function TranscribeSettingsPanel({ projectId, config, onSaved }: TranscribeSettingsPanelProps) {
  const [batchSize, setBatchSize] = useState(config.whisper_batch_size)
  const [computeType, setComputeType] = useState(config.whisper_compute_type)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const dirty =
    batchSize !== config.whisper_batch_size || computeType !== config.whisper_compute_type

  async function handleSave() {
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      await patchProject(projectId, {
        whisper_batch_size: batchSize,
        whisper_compute_type: computeType,
      })
      onSaved()
      setSaved(true)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  function handleReset() {
    setBatchSize(config.whisper_batch_size)
    setComputeType(config.whisper_compute_type)
    setError(null)
    setSaved(false)
  }

  return (
    <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4 space-y-4 text-sm">
      <div className="grid grid-cols-2 gap-4">
        <div className="flex items-center gap-3">
          <label htmlFor="batch-size" className="shrink-0 font-medium text-gray-700 dark:text-gray-300">Batch size</label>
          <input
            id="batch-size"
            type="number"
            min={1}
            max={64}
            value={batchSize}
            onChange={(e) => {
              const v = parseInt(e.target.value, 10)
              setBatchSize(Number.isNaN(v) ? 1 : Math.min(64, Math.max(1, v)))
              setSaved(false)
            }}
            className="w-20 border border-gray-300 dark:border-gray-600 rounded px-2 py-1 dark:bg-gray-900 dark:text-gray-100"
          />
        </div>
        <div className="flex items-center gap-3">
          <label htmlFor="compute-type" className="shrink-0 font-medium text-gray-700 dark:text-gray-300">Precision</label>
          <select
            id="compute-type"
            value={computeType}
            onChange={(e) => { setComputeType(e.target.value); setSaved(false) }}
            className="flex-1 border border-gray-300 dark:border-gray-600 rounded px-2 py-1 dark:bg-gray-900 dark:text-gray-100"
          >
            {WHISPER_COMPUTE_TYPES.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </div>
      </div>
      <p className="text-xs text-gray-500 dark:text-gray-400">
        Lower the batch size or pick a lighter precision (e.g. <span className="font-mono">int8_float16</span>) if
        transcription runs out of GPU memory.
      </p>

      {error && (
        <p className="text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg px-3 py-2">
          {error}
        </p>
      )}
      {saved && !dirty && (
        <p className="text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg px-3 py-2">
          Saved.
        </p>
      )}

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => void handleSave()}
          disabled={!dirty || saving}
          className="px-4 py-2 font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? 'Saving…' : 'Save settings'}
        </button>
        <button
          type="button"
          onClick={handleReset}
          disabled={!dirty || saving}
          className="px-4 py-2 font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-50"
        >
          Reset
        </button>
      </div>
    </div>
  )
}
