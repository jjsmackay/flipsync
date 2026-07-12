import { useState } from 'react'
import type { JobSummary, FailedJob } from '../../types/api'
import { ProgressBar } from '../ui/ProgressBar'

interface JobsPanelProps {
  activeJobs: JobSummary[]
  failedJobs: FailedJob[]
  onRetry?: (job: FailedJob) => void
  retryingJobId?: string | null
}

const JOB_LABELS: Record<string, string> = {
  extract_audio: 'Extracting audio',
  vocal_separation: 'Vocal separation',
  diarisation: 'Diarisation',
  transcription: 'Transcription',
  cleanup: 'Cleanup',
  export: 'Export',
}

function jobLabel(type: string): string {
  return JOB_LABELS[type] ?? type.replace(/_/g, ' ')
}

function formatTime(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function JobsPanel({ activeJobs, failedJobs, onRetry, retryingJobId }: JobsPanelProps) {
  // Dismissed failed jobs are hidden locally; the API keeps returning them, so this
  // is a per-session hide (spec: shown until dismissed or retried).
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(new Set())

  const visibleFailed = failedJobs.filter((job) => !dismissedIds.has(job.id))

  function dismiss(jobId: string) {
    setDismissedIds((prev) => {
      const next = new Set(prev)
      next.add(jobId)
      return next
    })
  }

  if (activeJobs.length === 0 && visibleFailed.length === 0) {
    return null
  }

  return (
    <div className="space-y-3">
      {activeJobs.map((job) => (
        <div key={job.id} className="bg-blue-50 border border-blue-100 rounded-lg p-3">
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm font-medium text-blue-800">{jobLabel(job.type)}</span>
            <span className="text-xs text-blue-500 capitalize">{job.status}</span>
          </div>
          {job.progress !== null && (
            <ProgressBar value={job.progress} color="blue" />
          )}
        </div>
      ))}

      {visibleFailed.map((job) => (
        <div key={job.id} className="bg-red-50 border border-red-100 rounded-lg p-3">
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm font-medium text-red-800">{jobLabel(job.type)}</span>
            {job.completed_at && (
              <span className="text-xs text-red-400">{formatTime(job.completed_at)}</span>
            )}
          </div>
          {job.error && (
            <p className="text-xs text-red-600 mt-1">{job.error}</p>
          )}
          <div className="flex items-center gap-2 mt-2">
            {onRetry && (
              <button
                type="button"
                onClick={() => onRetry(job)}
                disabled={retryingJobId === job.id}
                className="text-xs px-2 py-1 rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {retryingJobId === job.id ? 'Retrying…' : 'Retry'}
              </button>
            )}
            <button
              type="button"
              onClick={() => dismiss(job.id)}
              className="text-xs px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-100"
            >
              Dismiss
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}
