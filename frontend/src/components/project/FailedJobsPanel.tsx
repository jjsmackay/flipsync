import { useEffect, useState } from 'react'
import type { FailedJob } from '../../types/api'
import { jobLabel } from '../../utils/labels'
import { retryPlan, retryGuidance } from '../../utils/retry'

interface FailedJobsPanelProps {
  failedJobs: FailedJob[]
  onRetry?: (job: FailedJob) => void
  retryingJobId?: string | null
}

// Dismissals persist across reloads: the API keeps returning failed jobs, so a
// purely in-memory hide reappeared on refresh. This is UI dismissal state, not
// segment state. localStorage can throw (private mode) — degrade to in-memory.
const STORAGE_KEY = 'flipsync:dismissedFailedJobs'

function readDismissed(): Set<string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) return new Set(JSON.parse(raw) as string[])
  } catch {
    /* ignore */
  }
  return new Set()
}

function writeDismissed(ids: Set<string>): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([...ids]))
  } catch {
    /* ignore */
  }
}

function formatTime(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function FailedJobsPanel({ failedJobs, onRetry, retryingJobId }: FailedJobsPanelProps) {
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(readDismissed)

  // Prune stored ids no longer returned by the API (retried/resolved jobs) so the
  // list can't grow unbounded.
  useEffect(() => {
    const live = new Set(failedJobs.map((j) => j.id))
    setDismissedIds((prev) => {
      const pruned = new Set([...prev].filter((id) => live.has(id)))
      if (pruned.size !== prev.size) {
        writeDismissed(pruned)
        return pruned
      }
      return prev
    })
  }, [failedJobs])

  const visibleFailed = failedJobs.filter((job) => !dismissedIds.has(job.id))

  function dismiss(jobId: string) {
    setDismissedIds((prev) => {
      const next = new Set(prev)
      next.add(jobId)
      writeDismissed(next)
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
          {retryGuidance(job.type) && (
            <p className="text-xs text-red-700 dark:text-red-300 mt-1">{retryGuidance(job.type)}</p>
          )}
          <div className="flex items-center gap-2 mt-2">
            {onRetry && retryPlan(job) !== null && (
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
