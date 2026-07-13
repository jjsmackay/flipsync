import { useRef, useState, ChangeEvent } from 'react'
import type { ProjectDetail } from '../../types/api'
import { startPipeline, uploadReference } from '../../api/client'
import { errorMessage } from '../../utils/errors'
import { jobLabel } from '../../utils/labels'
import { ProgressBar } from '../ui/ProgressBar'
import { SpeakerScanPicker, SCOUTABLE_STATUSES } from './SpeakerScanPicker'

// Re-exported for backwards compat — referencePlan now lives with the rest of
// the scan machinery in SpeakerScanPicker.
export { referencePlan } from './SpeakerScanPicker'

// The Speaker stage runs as a small state machine. Because the prompt is the
// only trigger for separation (and the upload-clip path sets a reference before
// separation runs), this whole component only ever renders while no reference
// is set — deriveStage guarantees it.
type Phase = 'preparing' | 'prompt' | 'separating' | 'failed' | 'scan'

interface SetReferencePanelProps {
  project: ProjectDetail
  onAction: () => void
  pollIntervalMs?: number
}

function derivePhase(project: ProjectDetail): Phase {
  const sources = project.stats.source_coverage
  // A source with a ready vocals stem — scan and pick a voice.
  if (sources.some((s) => SCOUTABLE_STATUSES.has(s.status))) return 'scan'
  // Separation running for the scan.
  if (
    project.active_jobs.some((j) => j.type === 'vocal_separation') ||
    sources.some((s) => s.status === 'separation_running')
  ) {
    return 'separating'
  }
  // A step failed before we got a reference — the FailedJobsPanel owns retry.
  if (sources.some((s) => s.status === 'extraction_failed' || s.status === 'separation_failed')) {
    return 'failed'
  }
  // Audio extracted, ready to ask whose voice we're after.
  if (sources.some((s) => s.status === 'separation_pending')) return 'prompt'
  // Still uploading / extracting.
  return 'preparing'
}

function Heading({ title, blurb }: { title: string; blurb?: string }) {
  return (
    <div className="mb-4">
      <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">{title}</h3>
      {blurb && <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">{blurb}</p>}
    </div>
  )
}

export function SetReferencePanel({ project, onAction, pollIntervalMs = 3000 }: SetReferencePanelProps) {
  const phase = derivePhase(project)

  const scoutable = project.stats.source_coverage.filter((s) => SCOUTABLE_STATUSES.has(s.status))
  const autoSourceId = scoutable[0]?.source_id ?? ''

  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)

  const [uploadProgress, setUploadProgress] = useState<number | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const uploadInputRef = useRef<HTMLInputElement>(null)

  async function handleFindSpeakers() {
    setStartError(null)
    setStarting(true)
    try {
      await startPipeline(project.id)
      onAction()
    } catch (err) {
      setStartError(errorMessage(err, 'Failed to start processing'))
    } finally {
      setStarting(false)
    }
  }

  async function handleUpload(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setUploadError(null)
    setUploadProgress(0)
    try {
      await uploadReference(project.id, file, (f) => setUploadProgress(f))
      onAction()
    } catch (err) {
      setUploadError(errorMessage(err, 'Upload failed'))
    } finally {
      setUploadProgress(null)
    }
  }

  const separationJob = project.active_jobs.find((j) => j.type === 'vocal_separation')

  if (phase === 'preparing') {
    return (
      <div>
        <Heading title="Getting your video ready" blurb="Extracting the audio so we can isolate the vocals." />
        <p className="text-sm text-gray-600 dark:text-gray-400">This only takes a moment…</p>
      </div>
    )
  }

  if (phase === 'failed') {
    return (
      <div>
        <Heading
          title="Something went wrong"
          blurb="Preparing your video failed — retry it from the alert below."
        />
      </div>
    )
  }

  if (phase === 'separating') {
    return (
      <div>
        <Heading
          title="Finding the speakers"
          blurb="Isolating the vocals so we can scan for voices. This can take a while — you can leave this page."
        />
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm font-medium text-blue-800 dark:text-blue-300">
              {jobLabel('vocal_separation')}
            </span>
            {separationJob?.status && (
              <span className="text-xs text-blue-500 dark:text-blue-400 capitalize">{separationJob.status}</span>
            )}
          </div>
          {separationJob?.progress != null && <ProgressBar value={separationJob.progress} color="blue" />}
        </div>
      </div>
    )
  }

  if (phase === 'prompt') {
    return (
      <div>
        <Heading
          title="Whose voice are we after?"
          blurb="Scan your video for the speakers in it, or upload a short clean clip of the target voice."
        />
        <div className="flex flex-wrap gap-3">
          <button
            type="button"
            onClick={() => void handleFindSpeakers()}
            disabled={starting || uploadProgress != null}
            className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg
              hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {starting ? 'Starting…' : 'Find speakers'}
          </button>
          <button
            type="button"
            onClick={() => uploadProgress == null && uploadInputRef.current?.click()}
            disabled={starting || uploadProgress != null}
            className="px-4 py-2 border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 text-sm font-medium rounded-lg
              hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {uploadProgress != null ? 'Uploading…' : 'Upload a clip'}
          </button>
          <input
            ref={uploadInputRef}
            type="file"
            accept="audio/*"
            className="hidden"
            onChange={(e) => void handleUpload(e)}
            disabled={uploadProgress != null}
          />
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
        {startError && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{startError}</p>}
        {uploadError && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{uploadError}</p>}
      </div>
    )
  }

  // phase === 'scan'
  return (
    <div>
      <Heading
        title="Whose voice are we after?"
        blurb="We scanned your video for voices — pick the target speaker below, or upload a clip instead."
      />
      <SpeakerScanPicker
        projectId={project.id}
        autoSourceId={autoSourceId}
        onSelected={onAction}
        pollIntervalMs={pollIntervalMs}
        autoScan
      />
    </div>
  )
}
