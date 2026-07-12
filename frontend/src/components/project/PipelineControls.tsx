import { useState } from 'react'
import type { ProjectDetail } from '../../types/api'
import { startPipeline, runTranscription } from '../../api/client'

interface PipelineControlsProps {
  project: ProjectDetail
  onAction: () => void
}

export function PipelineControls({ project, onAction }: PipelineControlsProps) {
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const hasActiveJobs = project.active_jobs.length > 0

  const hasStep1Pending = project.stats.source_coverage.some(
    (s) => s.status === 'step1_pending',
  )

  const canStartPipeline = hasStep1Pending && !hasActiveJobs
  const canRunTranscription = !hasActiveJobs

  async function handleStartPipeline() {
    setError(null)
    setLoading(true)
    try {
      await startPipeline(project.id)
      onAction()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start pipeline')
    } finally {
      setLoading(false)
    }
  }

  async function handleRunTranscription() {
    setError(null)
    setLoading(true)
    try {
      await runTranscription(project.id)
      onAction()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to run transcription')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-3">
        <button
          onClick={handleStartPipeline}
          disabled={!canStartPipeline || loading}
          className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg
            hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          Start pipeline
        </button>
        <button
          onClick={handleRunTranscription}
          disabled={!canRunTranscription || loading}
          className="px-4 py-2 bg-gray-600 text-white text-sm font-medium rounded-lg
            hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          Run transcription
        </button>
      </div>

      {error && (
        <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
      )}
    </div>
  )
}
