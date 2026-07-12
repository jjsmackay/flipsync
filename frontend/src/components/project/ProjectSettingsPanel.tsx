import { useState } from 'react'
import type { ProjectConfig } from '../../types/api'
import { patchProject, ApiError } from '../../api/client'

interface ProjectSettingsPanelProps {
  projectId: string
  config: ProjectConfig
  /** Called after a successful save so the parent can refetch — stats move immediately
   * because the orchestrator re-evaluates segment statuses synchronously. */
  onSaved: () => void
}

export function ProjectSettingsPanel({ projectId, config, onSaved }: ProjectSettingsPanelProps) {
  const [matchThreshold, setMatchThreshold] = useState(config.match_threshold)
  const [autoApproveEnabled, setAutoApproveEnabled] = useState(config.auto_approve_enabled)
  const [autoApproveMatchThreshold, setAutoApproveMatchThreshold] = useState(
    config.auto_approve_match_threshold,
  )
  const [autoApproveTranscriptThreshold, setAutoApproveTranscriptThreshold] = useState(
    config.auto_approve_transcript_threshold,
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const dirty =
    matchThreshold !== config.match_threshold ||
    autoApproveEnabled !== config.auto_approve_enabled ||
    autoApproveMatchThreshold !== config.auto_approve_match_threshold ||
    autoApproveTranscriptThreshold !== config.auto_approve_transcript_threshold

  async function handleSave() {
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      await patchProject(projectId, {
        match_threshold: matchThreshold,
        auto_approve_enabled: autoApproveEnabled,
        auto_approve_match_threshold: autoApproveMatchThreshold,
        auto_approve_transcript_threshold: autoApproveTranscriptThreshold,
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
    setMatchThreshold(config.match_threshold)
    setAutoApproveEnabled(config.auto_approve_enabled)
    setAutoApproveMatchThreshold(config.auto_approve_match_threshold)
    setAutoApproveTranscriptThreshold(config.auto_approve_transcript_threshold)
    setError(null)
    setSaved(false)
  }

  return (
    <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-5 space-y-5">
      {/* Match threshold */}
      <div>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
          Match threshold
          <span className="ml-2 font-mono text-blue-600">{matchThreshold.toFixed(2)}</span>
        </label>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={matchThreshold}
          onChange={(e) => { setMatchThreshold(parseFloat(e.target.value)); setSaved(false) }}
          className="w-full accent-blue-600"
        />
        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
          Segments below this speaker-match score are held as below threshold. Changing it
          re-evaluates every segment immediately.
        </p>
      </div>

      <hr className="border-gray-100 dark:border-gray-800" />

      {/* Auto-approve toggle */}
      <div>
        <label className="flex items-center gap-2 text-sm font-medium text-gray-700 dark:text-gray-300 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={autoApproveEnabled}
            onChange={(e) => { setAutoApproveEnabled(e.target.checked); setSaved(false) }}
            className="accent-teal-600 w-4 h-4"
          />
          Auto-approve
        </label>
        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
          Segments that clear both thresholds below move straight to
          <span className="mx-1 inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-teal-100 dark:bg-teal-900/40 text-teal-700 dark:text-teal-300">
            auto-approved
          </span>
          — included in export and approved duration, but freely demotable in review.
        </p>
      </div>

      <div className={autoApproveEnabled ? '' : 'opacity-50 pointer-events-none'}>
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Auto-approve match threshold
            <span className="ml-2 font-mono text-teal-700">{autoApproveMatchThreshold.toFixed(2)}</span>
          </label>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={autoApproveMatchThreshold}
            onChange={(e) => { setAutoApproveMatchThreshold(parseFloat(e.target.value)); setSaved(false) }}
            className="w-full accent-teal-600"
            disabled={!autoApproveEnabled}
          />
        </div>

        <div className="mt-3">
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Auto-approve transcript threshold
            <span className="ml-2 font-mono text-teal-700">{autoApproveTranscriptThreshold.toFixed(2)}</span>
          </label>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={autoApproveTranscriptThreshold}
            onChange={(e) => { setAutoApproveTranscriptThreshold(parseFloat(e.target.value)); setSaved(false) }}
            className="w-full accent-teal-600"
            disabled={!autoApproveEnabled}
          />
        </div>
      </div>

      {error && (
        <p className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg px-3 py-2">
          {error}
        </p>
      )}
      {saved && !dirty && (
        <p className="text-sm text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg px-3 py-2">
          Saved — segment statuses have been re-evaluated.
        </p>
      )}

      <div className="flex justify-end gap-3">
        <button
          type="button"
          onClick={handleReset}
          disabled={!dirty || saving}
          className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-50"
        >
          Reset
        </button>
        <button
          type="button"
          onClick={() => void handleSave()}
          disabled={!dirty || saving}
          className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? 'Saving…' : 'Save settings'}
        </button>
      </div>
    </div>
  )
}
