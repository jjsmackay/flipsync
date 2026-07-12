import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useProjectPolling } from '../hooks/useProjectPolling'
import { reprocessSource, runTranscription, triggerExport, ApiError } from '../api/client'
import type { FailedJob } from '../types/api'
import { StatusBadge } from '../components/ui/StatusBadge'
import { JobsPanel } from '../components/project/JobsPanel'
import { StatsPanel } from '../components/project/StatsPanel'
import { ProjectSettingsPanel } from '../components/project/ProjectSettingsPanel'
import { PipelineControls } from '../components/project/PipelineControls'
import { SetReferencePanel } from '../components/project/SetReferencePanel'
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
        // vocal_separation / extract_audio re-run step 1; diarisation re-runs step 2.
        const steps = job.type === 'diarisation' ? ['step2'] : ['step1']
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

  return (
    <div className="max-w-4xl mx-auto px-6 py-8 space-y-8">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 truncate">{project.name}</h1>
          <StatusBadge status={project.status} />
        </div>
        <div className="flex items-center gap-3 flex-shrink-0">
          <ThemeToggle />
          <Link
            to={`/projects/${project.id}/review`}
            className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg
              hover:bg-blue-700 transition-colors"
          >
            Review queue →
          </Link>
        </div>
      </div>

      {/* Active & Failed Jobs */}
      {(project.active_jobs.length > 0 || project.recent_failed_jobs.length > 0) && (
        <Section title="Jobs">
          <JobsPanel
            activeJobs={project.active_jobs}
            failedJobs={project.recent_failed_jobs}
            onRetry={handleRetryJob}
            retryingJobId={retryingJobId}
          />
        </Section>
      )}

      {/* Stats */}
      <Section title="Stats">
        <StatsPanel stats={project.stats} config={project.config} />
      </Section>

      {/* Set reference — the pipeline gate. Shown when step 1 has produced a
          stem (a source is at step2_pending) and nothing is running. Keyed on
          the source state, not status === 'awaiting_reference', because picking
          a reference recomputes the project to 'ready' before the user clicks
          Continue and the panel must stay up. */}
      {project.active_jobs.length === 0 &&
        project.stats.source_coverage.some((s) => s.status === 'step2_pending') && (
          <Section title="Set reference">
            <SetReferencePanel project={project} onAction={() => void refetch()} />
          </Section>
        )}

      {/* Settings */}
      <Section title="Settings">
        <ProjectSettingsPanel
          projectId={project.id}
          config={project.config}
          onSaved={() => void refetch()}
        />
      </Section>

      {/* Pipeline Controls */}
      <Section title="Pipeline">
        <PipelineControls project={project} onAction={() => void refetch()} />
      </Section>

      {/* Sources */}
      <Section title="Sources">
        <SourcesTable
          sources={project.stats.source_coverage}
          onReprocess={handleReprocess}
        />
        {reprocessError && (
          <p className="mt-3 text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded px-3 py-2">
            {reprocessError}
          </p>
        )}
      </Section>

      {/* Upload */}
      <Section title="Upload">
        <UploadArea projectId={project.id} onUploaded={() => void refetch()} />
      </Section>

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
