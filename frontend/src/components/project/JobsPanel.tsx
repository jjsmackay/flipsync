import type { JobSummary, FailedJob } from '../../types/api'
import { ProgressBar } from '../ui/ProgressBar'

interface JobsPanelProps {
  activeJobs: JobSummary[]
  failedJobs: FailedJob[]
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

export function JobsPanel({ activeJobs, failedJobs }: JobsPanelProps) {
  if (activeJobs.length === 0 && failedJobs.length === 0) {
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

      {failedJobs.map((job) => (
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
        </div>
      ))}
    </div>
  )
}
