import { useCallback, useEffect, useState } from 'react'
import type { ProjectDetail, Model } from '../../types/api'
import { getModels } from '../../api/client'
import { TrainPanel } from './TrainPanel'
import { ModelsList } from './ModelsList'
import { PreviewPanel } from './PreviewPanel'

interface VoiceSectionProps {
  project: ProjectDetail
  /** Refetch the project (drives job polling). */
  refetch: () => void
}

function SubSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide">{title}</h3>
      {children}
    </div>
  )
}

export function VoiceSection({ project, refetch }: VoiceSectionProps) {
  const [models, setModels] = useState<Model[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const reloadModels = useCallback(async () => {
    try {
      const res = await getModels(project.id)
      setModels(res.models)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load models.')
    } finally {
      setLoading(false)
    }
  }, [project.id])

  // Reload on mount and whenever a dataset-build/fine-tune job starts or finishes —
  // model rows change status (pending → training → ready|failed) around those edges.
  const voiceJobActive = project.active_jobs.some(
    (j) => j.type === 'finetune' || j.type === 'dataset_build',
  )
  useEffect(() => {
    void reloadModels()
  }, [reloadModels, voiceJobActive])

  function handleStarted() {
    refetch()
    void reloadModels()
  }

  return (
    <div className="space-y-6">
      <SubSection title="Train">
        <TrainPanel project={project} models={models} onStarted={handleStarted} />
      </SubSection>

      <SubSection title="Models">
        <ModelsList
          projectId={project.id}
          models={models}
          loading={loading}
          error={error}
          onChanged={() => void reloadModels()}
        />
      </SubSection>

      <SubSection title="Preview">
        <PreviewPanel projectId={project.id} models={models} />
      </SubSection>
    </div>
  )
}
