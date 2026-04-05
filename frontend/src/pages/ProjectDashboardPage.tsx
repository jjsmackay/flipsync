import { useParams, Link } from 'react-router-dom'
import { useProjectPolling } from '../hooks/useProjectPolling'
import { reprocessSource } from '../api/client'
import { StatusBadge } from '../components/ui/StatusBadge'
import { JobsPanel } from '../components/project/JobsPanel'
import { StatsPanel } from '../components/project/StatsPanel'
import { PipelineControls } from '../components/project/PipelineControls'
import { SourcesTable } from '../components/project/SourcesTable'
import { UploadArea } from '../components/project/UploadArea'

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide mb-3">{title}</h2>
      {children}
    </section>
  )
}

export function ProjectDashboardPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const { project, isLoading, error } = useProjectPolling(projectId!)

  async function handleReprocess(sourceId: string, steps: string[]) {
    if (!projectId) return
    await reprocessSource(projectId, sourceId, steps, undefined, true)
    // polling will pick up changes automatically
  }

  if (isLoading && !project) {
    return (
      <div className="p-8 text-gray-500 text-sm">Loading project...</div>
    )
  }

  if (error) {
    return (
      <div className="p-8 text-red-600 text-sm">
        Failed to load project: {error.message}
      </div>
    )
  }

  if (!project) {
    return (
      <div className="p-8 text-gray-500 text-sm">Project not found.</div>
    )
  }

  return (
    <div className="max-w-4xl mx-auto px-6 py-8 space-y-8">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          <h1 className="text-2xl font-bold text-gray-900 truncate">{project.name}</h1>
          <StatusBadge status={project.status} />
        </div>
        <Link
          to={`/projects/${project.id}/review`}
          className="flex-shrink-0 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg
            hover:bg-blue-700 transition-colors"
        >
          Review queue →
        </Link>
      </div>

      {/* Active & Failed Jobs */}
      {(project.active_jobs.length > 0 || project.recent_failed_jobs.length > 0) && (
        <Section title="Jobs">
          <JobsPanel
            activeJobs={project.active_jobs}
            failedJobs={project.recent_failed_jobs}
          />
        </Section>
      )}

      {/* Stats */}
      <Section title="Stats">
        <StatsPanel stats={project.stats} config={project.config} />
      </Section>

      {/* Pipeline Controls */}
      <Section title="Pipeline">
        <PipelineControls project={project} onAction={() => {}} />
      </Section>

      {/* Sources */}
      <Section title="Sources">
        <SourcesTable
          sources={project.stats.source_coverage}
          onReprocess={handleReprocess}
        />
      </Section>

      {/* Upload */}
      <Section title="Upload">
        <UploadArea projectId={project.id} onUploaded={() => {}} />
      </Section>
    </div>
  )
}
