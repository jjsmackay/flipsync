import { useRef, useState, ChangeEvent } from 'react'
import type { ProjectDetail } from '../../types/api'
import { uploadReference, getReferenceAudioUrl, transcribeReference } from '../../api/client'
import { errorMessage } from '../../utils/errors'
import { SpeakerScanPicker, SCOUTABLE_STATUSES } from './SpeakerScanPicker'

interface ReferenceCardProps {
  project: ProjectDetail
  onAction: () => void
}

function provenanceLabel(project: ProjectDetail): string {
  const origin = project.reference_origin
  if (!origin) return 'Reference clip'
  if (origin.type === 'uploaded') return 'Uploaded clip'
  return `Picked ${origin.speaker_label} from a scan`
}

// Surfaces the project's current reference clip with playback, provenance, and
// two replace paths: upload a new clip, or re-pick from a speaker scan (the
// same SpeakerScanPicker the initial Speaker stage uses, expanded inline here).
export function ReferenceCard({ project, onAction }: ReferenceCardProps) {
  const [picking, setPicking] = useState(false)
  const [uploadProgress, setUploadProgress] = useState<number | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [transcribeError, setTranscribeError] = useState<string | null>(null)
  const uploadInputRef = useRef<HTMLInputElement>(null)

  const transcribing = project.active_jobs.some((j) => j.type === 'reference_transcribe')
  const transcript = project.reference_transcript?.trim() ?? ''

  async function handleTranscribe() {
    setTranscribeError(null)
    try {
      await transcribeReference(project.id)
      onAction()
    } catch (err) {
      setTranscribeError(errorMessage(err, 'Could not start transcription'))
    }
  }

  const scoutable = project.stats.source_coverage.filter((s) => SCOUTABLE_STATUSES.has(s.status))
  const autoSourceId = scoutable[0]?.source_id ?? ''

  async function handleUpload(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setUploadError(null)
    setUploadProgress(0)
    try {
      await uploadReference(project.id, file, (f) => setUploadProgress(f))
      setPicking(false)
      onAction()
    } catch (err) {
      setUploadError(errorMessage(err, 'Upload failed'))
    } finally {
      setUploadProgress(null)
    }
  }

  function handleSelected() {
    setPicking(false)
    onAction()
  }

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Reference</span>
          <p className="text-sm text-gray-700 dark:text-gray-300">{provenanceLabel(project)}</p>
          {/* Versioned on updated_at so a replaced reference isn't masked by the
              browser caching the previous clip at the same URL — replacing via
              upload leaves reference_origin (and the bare URL) unchanged. */}
          <audio
            key={project.updated_at}
            controls
            preload="none"
            src={`${getReferenceAudioUrl(project.id)}?v=${encodeURIComponent(project.updated_at)}`}
            className="mt-2 h-8 max-w-sm"
          />
        </div>
        <div className="flex flex-wrap items-center gap-3 shrink-0">
          <button
            type="button"
            onClick={() => uploadProgress == null && uploadInputRef.current?.click()}
            disabled={uploadProgress != null}
            className="px-3 py-1.5 border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 text-sm font-medium rounded-lg
              hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {uploadProgress != null ? 'Uploading…' : 'Replace with upload'}
          </button>
          <input
            ref={uploadInputRef}
            type="file"
            accept="audio/*"
            className="hidden"
            onChange={(e) => void handleUpload(e)}
            disabled={uploadProgress != null}
          />
          <button
            type="button"
            onClick={() => setPicking((p) => !p)}
            aria-expanded={picking}
            className="text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline"
          >
            {picking ? 'Cancel' : 'Pick from scan'}
          </button>
        </div>
      </div>

      {uploadProgress != null && (
        <div className="mt-3 w-full h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
          <div
            className={`h-full bg-blue-600 transition-[width] duration-150 ease-out
              ${uploadProgress >= 1 ? 'animate-pulse' : ''}`}
            style={{ width: `${Math.round(uploadProgress * 100)}%` }}
          />
        </div>
      )}
      {uploadError && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{uploadError}</p>}

      <p className="mt-3 text-xs text-gray-400 dark:text-gray-500">
        Replacing the reference doesn't re-match existing segments — reprocess sources to apply it.
      </p>

      {/* Reference transcript — read-only. Auto-transcribed when the reference
          is set; the button re-runs it (or recovers a reference set before this
          existed / while the transcription service was down). */}
      <div className="mt-3 border-t border-gray-100 dark:border-gray-800 pt-3">
        <div className="flex items-center justify-between gap-3">
          <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Transcript</span>
          {transcribing ? (
            <span className="text-xs text-gray-400 dark:text-gray-500">Transcribing…</span>
          ) : (
            <button
              type="button"
              onClick={() => void handleTranscribe()}
              className="text-xs font-medium text-blue-600 dark:text-blue-400 hover:underline"
            >
              {transcript ? 'Re-transcribe' : 'Transcribe'}
            </button>
          )}
        </div>
        {transcript ? (
          <p className="mt-1 text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">{transcript}</p>
        ) : (
          !transcribing && (
            <p className="mt-1 text-xs text-gray-400 dark:text-gray-500 italic">
              No transcript yet.
            </p>
          )
        )}
        {transcribeError && (
          <p className="mt-1 text-sm text-red-600 dark:text-red-400">{transcribeError}</p>
        )}
      </div>

      {picking && (
        <div className="mt-4 border-t border-gray-100 dark:border-gray-800 pt-4">
          <SpeakerScanPicker
            projectId={project.id}
            autoSourceId={autoSourceId}
            onSelected={handleSelected}
            autoScan
          />
        </div>
      )}
    </div>
  )
}
