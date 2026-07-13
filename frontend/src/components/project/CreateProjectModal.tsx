import { useState } from 'react'
import { createProject } from '../../api/client'
import type { CreateProjectRequest } from '../../types/api'
import { errorMessage } from '../../utils/errors'
import {
  changedValues,
  SEPARATION_KNOBS,
  DIARISATION_KNOBS,
  TRANSCRIPTION_KNOBS,
  CLEANUP_KNOBS,
  XTTS_KNOBS,
  TUNING_DEFAULTS,
  type Knob,
  type TuningKey,
  type TuningValue,
  type TuningValues,
} from '../../utils/tuning'
import { KnobFields } from './KnobFields'

const ALL_TUNING_KNOBS: Knob[] = [
  ...SEPARATION_KNOBS,
  ...DIARISATION_KNOBS,
  ...TRANSCRIPTION_KNOBS,
  ...CLEANUP_KNOBS,
  ...XTTS_KNOBS,
]

interface CreateProjectModalProps {
  onCreated: (id: string) => void
  onClose: () => void
}

const WHISPER_MODELS = ['tiny', 'base', 'small', 'medium', 'large-v2', 'large-v3'] as const
const LANGUAGES = [
  { value: 'auto', label: 'Auto-detect' },
  { value: 'en', label: 'English' },
  { value: 'fr', label: 'French' },
  { value: 'de', label: 'German' },
  { value: 'es', label: 'Spanish' },
  { value: 'ja', label: 'Japanese' },
  { value: 'zh', label: 'Chinese' },
] as const

export function CreateProjectModal({ onCreated, onClose }: CreateProjectModalProps) {
  const [name, setName] = useState('')
  const [whisperModel, setWhisperModel] = useState<string>('large-v3')
  const [language, setLanguage] = useState<string>('en')
  const [matchThreshold, setMatchThreshold] = useState(0.75)
  const [targetMinutes, setTargetMinutes] = useState(30)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tuningValues, setTuningValues] = useState<TuningValues>(TUNING_DEFAULTS)

  function handleTuningChange(key: TuningKey, value: TuningValue) {
    setTuningValues((prev) => ({ ...prev, [key]: value }))
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim()) return

    setSubmitting(true)
    setError(null)
    try {
      const req = {
        name: name.trim(),
        whisper_model: whisperModel,
        // "auto" is a UI-only sentinel; the API expects null for auto-detect.
        language: language === 'auto' ? null : language,
        match_threshold: matchThreshold,
        target_duration_secs: Math.round(targetMinutes * 60),
        // The knob values are typed loosely (TuningValue = number | string | boolean)
        // since KnobFields is shared across select/number/checkbox knobs; each field's
        // actual runtime type matches its knob kind, so the cast to the request's
        // per-field types is safe.
        ...changedValues(ALL_TUNING_KNOBS, tuningValues, TUNING_DEFAULTS),
      } as CreateProjectRequest
      const result = await createProject(req)
      onCreated(result.id)
    } catch (err) {
      setError(errorMessage(err, 'Failed to create project'))
    } finally {
      setSubmitting(false)
    }
  }

  function handleBackdropClick(e: React.MouseEvent<HTMLDivElement>) {
    if (e.target === e.currentTarget) {
      onClose()
    }
  }

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
      onClick={handleBackdropClick}
    >
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-md mx-4 p-6">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-5">New project</h2>

        {/* noValidate: validation is handled in handleSubmit — native constraint
            validation would otherwise also cover the collapsed Advanced knobs
            (e.g. number step mismatches), which shouldn't block submission while hidden. */}
        <form onSubmit={handleSubmit} noValidate className="space-y-4">
          {/* Name */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Project name <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. My Speaker Dataset"
              required
              className="w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              One speaker per project. Name it after the voice you're capturing.
            </p>
          </div>

          {/* Whisper model */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Whisper model
            </label>
            <select
              value={whisperModel}
              onChange={(e) => setWhisperModel(e.target.value)}
              className="w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            >
              {WHISPER_MODELS.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Transcription accuracy vs. speed. Larger is more accurate but slower and needs more VRAM.
              <code className="mx-0.5">large-v3</code> is recommended; drop to <code className="mx-0.5">medium</code> or
              <code className="mx-0.5">small</code> if you're VRAM-limited.
            </p>
          </div>

          {/* Language */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Language
            </label>
            <select
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
              className="w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            >
              {LANGUAGES.map((l) => (
                <option key={l.value} value={l.value}>{l.label}</option>
              ))}
            </select>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              The spoken language of your source audio. Set it explicitly when you know it —
              auto-detect can misfire on short or noisy clips.
            </p>
          </div>

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
              onChange={(e) => setMatchThreshold(parseFloat(e.target.value))}
              className="w-full accent-blue-600"
            />
            <div className="flex justify-between text-xs text-gray-400 dark:text-gray-500 mt-0.5">
              <span>0.00</span>
              <span>1.00</span>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              How closely a segment must match your reference clip to be kept. Higher = stricter
              (fewer, cleaner matches); lower surfaces more borderline segments to review.
              You can adjust this later. Default <span className="font-mono">0.75</span>.
            </p>
          </div>

          {/* Target duration */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Target duration (minutes)
            </label>
            <input
              type="number"
              min={1}
              step={1}
              value={targetMinutes}
              onChange={(e) => setTargetMinutes(parseInt(e.target.value, 10) || 30)}
              className="w-full border border-gray-300 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              How much approved audio you're aiming to collect — just a progress target, not a limit.
              Most voice-cloning datasets want 30+ minutes of clean speech.
            </p>
          </div>

          {/* Advanced tuning knobs */}
          <details className="group">
            <summary className="cursor-pointer select-none text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition-colors list-none flex items-center gap-1">
              <span className="inline-block transition-transform group-open:rotate-90">▸</span>
              Advanced
            </summary>
            <div className="mt-3 space-y-4">
              <div>
                <p className="text-xs font-semibold uppercase text-gray-500 dark:text-gray-400 mb-1.5">
                  Separation
                </p>
                <KnobFields
                  knobs={SEPARATION_KNOBS}
                  values={tuningValues}
                  onChange={handleTuningChange}
                  idPrefix="create-separation"
                />
              </div>
              <div>
                <p className="text-xs font-semibold uppercase text-gray-500 dark:text-gray-400 mb-1.5">
                  Speaker matching
                </p>
                <KnobFields
                  knobs={DIARISATION_KNOBS}
                  values={tuningValues}
                  onChange={handleTuningChange}
                  idPrefix="create-diarisation"
                />
              </div>
              <div>
                <p className="text-xs font-semibold uppercase text-gray-500 dark:text-gray-400 mb-1.5">
                  Transcription
                </p>
                <KnobFields
                  knobs={TRANSCRIPTION_KNOBS}
                  values={tuningValues}
                  onChange={handleTuningChange}
                  idPrefix="create-transcription"
                />
              </div>
              <div>
                <p className="text-xs font-semibold uppercase text-gray-500 dark:text-gray-400 mb-1.5">
                  Cleanup
                </p>
                <KnobFields
                  knobs={CLEANUP_KNOBS}
                  values={tuningValues}
                  onChange={handleTuningChange}
                  idPrefix="create-cleanup"
                />
              </div>
              <div>
                <p className="text-xs font-semibold uppercase text-gray-500 dark:text-gray-400 mb-1.5">
                  Voice training
                </p>
                <KnobFields
                  knobs={XTTS_KNOBS}
                  values={tuningValues}
                  onChange={handleTuningChange}
                  idPrefix="create-xtts"
                />
              </div>
            </div>
          </details>

          {/* Error */}
          {error && (
            <p className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg px-3 py-2">
              {error}
            </p>
          )}

          {/* Buttons */}
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting || !name.trim()}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting ? 'Creating…' : 'Create project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
