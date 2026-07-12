import { useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useProjectPolling } from '../hooks/useProjectPolling'
import { reprocessSource, runTranscription, triggerExport, ApiError } from '../api/client'
import type { FailedJob } from '../types/api'
import { FailedJobsPanel } from '../components/project/FailedJobsPanel'
import { StatsPanel } from '../components/project/StatsPanel'
import { ProjectSettingsPanel } from '../components/project/ProjectSettingsPanel'
import { StageStrip } from '../components/project/StageStrip'
import { NextActionCard } from '../components/project/NextActionCard'
import { SourcesTable } from '../components/project/SourcesTable'
import { UploadArea } from '../components/project/UploadArea'
import { ThemeToggle } from '../components/ui/ThemeToggle'

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h2 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-3">{title}</h2>
      {children}
    </section>
  )
}

interface ReprocessConfirm {
  sourceId: string
  steps: string[]
  message: string
}

export function ProjectDashboardPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const { project, isLoading, error, refetch } = useProjectPolling(projectId!)

  const [reprocessError, setReprocessError] = useState<string | null>(null)
  const [reprocessConfirm, setReprocessConfirm] = useState<ReprocessConfirm | null>(null)
  const [retryingJobId, setRetryingJobId] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const settingsRef = useRef<HTMLElement>(null)

  function openSettings() {
    setSettingsOpen(true)
    // Wait for the section to expand before scrolling to it.
    requestAnimationFrame(() => settingsRef.current?.scrollIntoView({ behavior: 'smooth' }))
  }

  async function submitReprocess(sourceId: string, steps: string[], confirm: boolean) {
    if (!projectId) return
    await reprocessSource(projectId, sourceId, steps, undefined, confirm)
    void refetch()
  }

  async function handleReprocess(sourceId: string, steps: string[]) {
    setReprocessError(null)
    try {
      await submitReprocess(sourceId, steps, false)
    } catch (err) {
      if (err instanceof ApiError && err.error === 'would_invalidate_approvals') {
        setReprocessConfirm({ sourceId, steps, message: err.message })
      } else {
        setReprocessError(err instanceof Error ? err.message : 'Reprocess failed')
      }
    }
  }

  async function handleConfirmReprocess() {
    if (!reprocessConfirm) return
    const { sourceId, steps } = reprocessConfirm
    setReprocessConfirm(null)
    setReprocessError(null)
    try {
      await submitReprocess(sourceId, steps, true)
    } catch (err) {
      setReprocessError(err instanceof Error ? err.message : 'Reprocess failed')
    }
  }

  async function handleRetryJob(job: FailedJob) {
    if (!projectId) return
    setReprocessError(null)
    setRetryingJobId(job.id)
    try {
      if (job.type === 'transcription_bulk' || job.type === 'transcription') {
        await runTranscription(projectId)
      } else if (job.type === 'export') {
        await triggerExport(projectId)
      } else if (job.source_id) {
        // vocal_separation / extract_audio re-run separation; diarisation re-runs itself.
        const steps = job.type === 'diarisation' ? ['diarisation'] : ['separation']
        await reprocessSource(projectId, job.source_id, steps, undefined, true)
      }
      void refetch()
    } catch (err) {
      setReprocessError(err instanceof Error ? err.message : 'Retry failed')
    } finally {
      setRetryingJobId(null)
    }
  }

  if (isLoading && !project) {
    return (
      <div className="p-8 text-gray-500 dark:text-gray-400 text-sm">Loading project...</div>
    )
  }

  if (error) {
    return (
      <div className="p-8 text-red-600 dark:text-red-400 text-sm">
        Failed to load project: {error.message}
      </div>
    )
  }

  if (!project) {
    return (
      <div className="p-8 text-gray-500 dark:text-gray-400 text-sm">Project not found.</div>
    )
  }

  const hasSources = project.stats.source_coverage.length > 0
  const hasSegments = project.stats.total_segments > 0

  return (
    <div className="max-w-4xl mx-auto px-6 py-8 space-y-8">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 truncate min-w-0">
          {project.name}
        </h1>
        <div className="flex items-center gap-3 flex-shrink-0">
          <ThemeToggle />
          <button
            onClick={openSettings}
            aria-label="Project settings"
            title="Project settings"
            className="p-2 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
          >
            ⚙
          </button>
        </div>
      </div>

      {/* Stage strip */}
      <StageStrip project={project} />

      {/* Next action */}
      <NextActionCard project={project} onAction={() => void refetch()} />

      {/* Failed jobs — own slot so appearing doesn't reflow the card */}
      {project.recent_failed_jobs.length > 0 && (
        <FailedJobsPanel
          failedJobs={project.recent_failed_jobs}
          onRetry={handleRetryJob}
          retryingJobId={retryingJobId}
        />
      )}

      {/* Sources */}
      {hasSources && (
        <Section title="Videos">
          <SourcesTable
            sources={project.stats.source_coverage}
            onReprocess={handleReprocess}
          />
          <div className="mt-3">
            <UploadArea projectId={project.id} onUploaded={() => void refetch()} compact />
          </div>
          {reprocessError && (
            <p className="mt-3 text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded px-3 py-2">
              {reprocessError}
            </p>
          )}
        </Section>
      )}

      {/* Stats */}
      {hasSegments && (
        <Section title="Segments">
          <StatsPanel stats={project.stats} config={project.config} />
        </Section>
      )}

      {/* Settings — collapsed by default */}
      <section ref={settingsRef}>
        <button
          onClick={() => setSettingsOpen(!settingsOpen)}
          className="flex items-center gap-2 text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-3 hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
        >
          <span className={`inline-block transition-transform ${settingsOpen ? 'rotate-90' : ''}`}>▸</span>
          Settings
        </button>
        {settingsOpen && (
          <ProjectSettingsPanel
            projectId={project.id}
            config={project.config}
            onSaved={() => void refetch()}
          />
        )}
      </section>

      {/* Reprocess confirmation */}
      {reprocessConfirm && (
        <div
          className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
          onClick={() => setReprocessConfirm(null)}
        >
          <div
            className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-md mx-4 p-6"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2">Confirm reprocess</h2>
            <p className="text-sm text-gray-600 dark:text-gray-400 mb-5">{reprocessConfirm.message}</p>
            <div className="flex justify-end gap-3">
              <button
                type="button"
                onClick={() => setReprocessConfirm(null)}
                className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void handleConfirmReprocess()}
                className="px-4 py-2 text-sm font-medium text-white bg-red-600 rounded-lg hover:bg-red-700"
              >
                Reprocess anyway
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
