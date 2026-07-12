import { useState } from 'react'
import type { FailedJob } from '../../types/api'
import { jobLabel } from '../../utils/labels'

interface FailedJobsPanelProps {
  failedJobs: FailedJob[]
  onRetry?: (job: FailedJob) => void
  retryingJobId?: string | null
}

function formatTime(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function FailedJobsPanel({ failedJobs, onRetry, retryingJobId }: FailedJobsPanelProps) {
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

  if (visibleFailed.length === 0) {
    return null
  }

  return (
    <div className="space-y-3">
      {visibleFailed.map((job) => (
        <div key={job.id} className="bg-red-50 dark:bg-red-900/20 border border-red-100 dark:border-red-800 rounded-lg p-3">
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm font-medium text-red-800 dark:text-red-300">
              {jobLabel(job.type)} failed
            </span>
            {job.completed_at && (
              <span className="text-xs text-red-400 dark:text-red-500">{formatTime(job.completed_at)}</span>
            )}
          </div>
          {job.error && (
            <p className="text-xs text-red-600 dark:text-red-400 mt-1">{job.error}</p>
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
              className="text-xs px-2 py-1 rounded border border-red-200 dark:border-red-800 text-red-600 dark:text-red-400 hover:bg-red-100 dark:hover:bg-red-900/30"
            >
              Dismiss
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}
