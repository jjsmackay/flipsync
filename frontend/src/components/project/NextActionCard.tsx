import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { ProjectDetail } from '../../types/api'
import { startPipeline, continuePipeline, runTranscription } from '../../api/client'
import { deriveStage } from '../../utils/stage'
import { jobLabel } from '../../utils/labels'
import { ProgressBar } from '../ui/ProgressBar'
import { UploadArea } from './UploadArea'
import { SetReferencePanel } from './SetReferencePanel'
import { ExportButton } from '../export/ExportButton'

interface NextActionCardProps {
  project: ProjectDetail
  onAction: () => void
}

// One card, one slot, always the same place on the page. Content follows the
// current stage; the reserved min-height keeps the 3s poll from shifting layout.
export function NextActionCard({ project, onAction }: NextActionCardProps) {
  const stage = deriveStage(project)

  return (
    <div
      className="min-h-[9rem] rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-5"
    >
      <div key={stage} className="fade-in">
        {stage === 'upload' && <UploadStage project={project} onAction={onAction} />}
        {stage === 'speaker' && <SpeakerStage project={project} onAction={onAction} />}
        {stage === 'process' && <ProcessStage project={project} onAction={onAction} />}
        {stage === 'review' && <ReviewStage project={project} onAction={onAction} />}
        {stage === 'export' && <ExportStage project={project} onAction={onAction} />}
      </div>
    </div>
  )
}

function StageHeading({ title, blurb }: { title: string; blurb?: string }) {
  return (
    <div className="mb-4">
      <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">{title}</h3>
      {blurb && <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">{blurb}</p>}
    </div>
  )
}

function UploadStage({ project, onAction }: NextActionCardProps) {
  return (
    <div>
      <StageHeading
        title="Upload a video to get started"
        blurb="FlipSync pulls out everything your speaker says and turns it into a voice dataset."
      />
      <UploadArea projectId={project.id} onUploaded={onAction} />
    </div>
  )
}

function SpeakerStage({ project, onAction }: NextActionCardProps) {
  return (
    <div>
      <StageHeading
        title="Whose voice are we after?"
        blurb="Scan a video to hear the speakers in it, or upload a short clean clip of the target voice."
      />
      <SetReferencePanel project={project} onAction={onAction} />
    </div>
  )
}

function ProcessStage({ project, onAction }: NextActionCardProps) {
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const activeJobs = project.active_jobs.filter(
    (j) => j.type !== 'export' && j.type !== 'scout_speakers',
  )
  const sources = project.stats.source_coverage
  const hasQueued = sources.some((s) => s.status === 'separation_pending')
  const gatedWithReference =
    sources.some((s) => s.status === 'diarisation_pending') && project.reference_path != null
  const hasFailure = sources.some((s) =>
    ['extraction_failed', 'separation_failed', 'diarisation_failed'].includes(s.status),
  )

  async function run(fn: (id: string) => Promise<unknown>) {
    setError(null)
    setLoading(true)
    try {
      await fn(project.id)
      onAction()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Request failed')
    } finally {
      setLoading(false)
    }
  }

  if (activeJobs.length > 0) {
    return (
      <div>
        <StageHeading title="Processing" blurb="This can take a while — you can leave this page." />
        <div className="space-y-3">
          {activeJobs.map((job) => (
            <div key={job.id}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium text-blue-800 dark:text-blue-300">
                  {jobLabel(job.type)}
                </span>
                <span className="text-xs text-blue-500 dark:text-blue-400 capitalize">{job.status}</span>
              </div>
              {job.progress !== null && <ProgressBar value={job.progress} color="blue" />}
            </div>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div>
      <StageHeading
        title={hasFailure && !hasQueued && !gatedWithReference ? 'Processing stopped' : 'Ready to process'}
        blurb={
          hasFailure && !hasQueued && !gatedWithReference
            ? 'A processing step failed — retry it from the alert below.'
            : gatedWithReference
            ? 'Reference is set. Continue to match your speaker through the uploaded videos.'
            : 'Separate the vocals and find your speaker in the uploaded videos.'
        }
      />
      <div className="flex flex-wrap gap-3">
        {hasQueued && (
          <button
            onClick={() => void run(startPipeline)}
            disabled={loading}
            className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg
              hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Start processing
          </button>
        )}
        {gatedWithReference && (
          <button
            onClick={() => void run(continuePipeline)}
            disabled={loading}
            className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg
              hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Continue processing
          </button>
        )}
      </div>
      {error && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{error}</p>}
    </div>
  )
}

function ReviewStage({ project, onAction }: NextActionCardProps) {
  const [error, setError] = useState<string | null>(null)
  const [transcribing, setTranscribing] = useState(false)

  const toReview = project.stats.pending_count + project.stats.maybe_count

  async function handleTranscribe() {
    setError(null)
    setTranscribing(true)
    try {
      await runTranscription(project.id)
      onAction()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start transcription')
    } finally {
      setTranscribing(false)
    }
  }

  return (
    <div>
      <StageHeading
        title={`${toReview} segment${toReview === 1 ? '' : 's'} ready to review`}
        blurb="Listen to each clip and approve the ones that belong in the dataset."
      />
      <div className="flex flex-wrap items-center gap-3">
        <Link
          to={`/projects/${project.id}/review`}
          className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg
            hover:bg-blue-700 transition-colors"
        >
          Start reviewing →
        </Link>
        <button
          onClick={() => void handleTranscribe()}
          disabled={transcribing}
          className="px-4 py-2 border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 text-sm font-medium rounded-lg
            hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {transcribing ? 'Starting…' : 'Transcribe segments'}
        </button>
      </div>
      {error && <p className="mt-3 text-sm text-red-600 dark:text-red-400">{error}</p>}
    </div>
  )
}

function ExportStage({ project, onAction }: NextActionCardProps) {
  const exportCount = project.stats.approved_count + project.stats.auto_approved_count
  return (
    <div>
      <StageHeading
        title={project.status === 'exported' ? 'Dataset exported' : 'Ready to export'}
        blurb={
          exportCount === 0
            ? 'Nothing approved yet — approve some segments in review first.'
            : project.status === 'exported'
            ? 'Download the dataset, or re-export after further review changes.'
            : 'Clean, normalise and package the approved segments into a dataset.'
        }
      />
      <ExportButton project={project} onStarted={onAction} />
    </div>
  )
}
