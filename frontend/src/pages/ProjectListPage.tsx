import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { usePolling } from '../hooks/usePolling'
import { getProjects, deleteProject } from '../api/client'
import { StatusBadge } from '../components/ui/StatusBadge'
import { CreateProjectModal } from '../components/project/CreateProjectModal'
import { ThemeToggle } from '../components/ui/ThemeToggle'
import type { ProjectSummary } from '../types/api'
import { formatDuration } from '../utils/format'


function formatDate(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

interface ProjectCardProps {
  project: ProjectSummary
  onDeleted: () => void
}

function ProjectCard({ project, onDeleted }: ProjectCardProps) {
  const navigate = useNavigate()
  const [deleteState, setDeleteState] = useState<'idle' | 'confirm' | 'deleting'>('idle')

  async function handleDelete(e: React.MouseEvent) {
    e.preventDefault()
    e.stopPropagation()

    if (deleteState === 'idle') {
      setDeleteState('confirm')
      return
    }

    if (deleteState === 'confirm') {
      setDeleteState('deleting')
      try {
        await deleteProject(project.id, true)
        onDeleted()
      } catch {
        setDeleteState('idle')
      }
    }
  }

  function handleCardClick() {
    navigate(`/projects/${project.id}`)
  }

  return (
    <div
      className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-5 hover:border-blue-300 dark:hover:border-blue-600 hover:shadow-sm transition-all cursor-pointer group"
      onClick={handleCardClick}
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-2 mb-3">
        <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate flex-1" title={project.name}>
          {project.name}
        </h3>
        <StatusBadge status={project.status} />
      </div>

      {/* Updated date */}
      <p className="text-xs text-gray-400 dark:text-gray-500 mb-4">
        Updated {formatDate(project.updated_at)}
      </p>

      {/* Stats */}
      <div className="grid grid-cols-3 gap-2 mb-4">
        <div className="text-center">
          <div className="text-lg font-semibold text-green-600">{project.stats.approved_count}</div>
          <div className="text-xs text-gray-500 dark:text-gray-400">approved</div>
        </div>
        <div className="text-center">
          <div className="text-lg font-semibold text-gray-600 dark:text-gray-400">{project.stats.pending_count}</div>
          <div className="text-xs text-gray-500 dark:text-gray-400">pending</div>
        </div>
        <div className="text-center">
          <div className="text-lg font-semibold text-blue-600">
            {formatDuration(project.stats.approved_duration_secs)}
          </div>
          <div className="text-xs text-gray-500 dark:text-gray-400">approved</div>
        </div>
      </div>

      {/* Progress toward target */}
      {project.target_duration_secs != null && project.target_duration_secs > 0 && (
        <div className="mb-4">
          <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
            <span>{formatDuration(project.stats.approved_duration_secs)}</span>
            <span>{formatDuration(project.target_duration_secs)} target</span>
          </div>
          <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-1.5">
            <div
              className="bg-green-500 h-1.5 rounded-full transition-all"
              style={{ width: `${Math.min(100, (project.stats.approved_duration_secs / project.target_duration_secs) * 100)}%` }}
            />
          </div>
        </div>
      )}

      {/* Footer row: delete button */}
      <div className="flex justify-end">
        <button
          onClick={handleDelete}
          disabled={deleteState === 'deleting'}
          className={`text-xs px-2 py-1 rounded transition-colors ${
            deleteState === 'confirm'
              ? 'bg-red-600 text-white hover:bg-red-700'
              : 'text-gray-400 dark:text-gray-500 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 opacity-0 group-hover:opacity-100'
          } disabled:opacity-50`}
        >
          {deleteState === 'deleting'
            ? 'Deleting…'
            : deleteState === 'confirm'
            ? 'Confirm delete'
            : 'Delete'}
        </button>
      </div>
    </div>
  )
}

export function ProjectListPage() {
  const navigate = useNavigate()
  const [showModal, setShowModal] = useState(false)

  const fetchFn = useCallback(() => getProjects(), [])

  const { data, error, isLoading, refetch } = usePolling(fetchFn, { intervalMs: 10000 })

  function handleCreated(id: string) {
    setShowModal(false)
    navigate(`/projects/${id}`)
  }

  function handleDeleted() {
    // Refetch immediately so the deleted card disappears without waiting for the poll.
    void refetch()
  }

  const projects = data?.projects ?? []

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900">
      {/* Header */}
      <div className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">FlipSync</h1>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">Voice dataset extraction</p>
          </div>
          <div className="flex items-center gap-3">
            <ThemeToggle />
            <button
              onClick={() => setShowModal(true)}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
            >
              + New project
            </button>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="max-w-6xl mx-auto px-6 py-8">
        {/* Loading state — only show when no data yet */}
        {isLoading && projects.length === 0 && (
          <div className="flex items-center justify-center py-20 text-gray-400 dark:text-gray-500">
            <svg className="animate-spin h-6 w-6 mr-2" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
              />
            </svg>
            Loading projects…
          </div>
        )}

        {/* Error state */}
        {error && !isLoading && (
          <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-xl px-5 py-4 text-sm text-red-700 dark:text-red-400">
            Failed to load projects: {error.message}
          </div>
        )}

        {/* Empty state */}
        {!isLoading && !error && projects.length === 0 && (
          <div className="flex flex-col items-center justify-center py-24 text-center">
            <div className="w-14 h-14 bg-gray-100 dark:bg-gray-800 rounded-2xl flex items-center justify-center mb-4">
              <svg className="w-7 h-7 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={1.5}
                  d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"
                />
              </svg>
            </div>
            <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100 mb-1">No projects yet</h2>
            <p className="text-sm text-gray-500 dark:text-gray-400 mb-6">Create your first project to start extracting voice datasets.</p>
            <button
              onClick={() => setShowModal(true)}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
            >
              + New project
            </button>
          </div>
        )}

        {/* Project grid */}
        {projects.length > 0 && (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {projects.map((project) => (
              <ProjectCard key={project.id} project={project} onDeleted={handleDeleted} />
            ))}
          </div>
        )}
      </div>

      {/* Create modal */}
      {showModal && (
        <CreateProjectModal
          onCreated={handleCreated}
          onClose={() => setShowModal(false)}
        />
      )}
    </div>
  )
}
