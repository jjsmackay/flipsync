import type { ProjectDetail, Model } from '../../types/api'
import { ModelsList } from './ModelsList'
import { PreviewPanel } from './PreviewPanel'

interface ModelsSectionProps {
  project: ProjectDetail
  // Models state is owned by the dashboard (the pipeline's Train row needs it
  // too); this section renders it and asks for reloads. Training itself lives
  // on the pipeline's Train row — this section is trained models + preview.
  models: Model[]
  modelsLoading: boolean
  modelsError: string | null
  reloadModels: () => void
}

// Deliberately one size down from the page's Section headings (text-sm) —
// same weight/tone/tracking so it reads as the tier below, not a variant.
function SubSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">{title}</h3>
      {children}
    </div>
  )
}

export function ModelsSection({
  project,
  models,
  modelsLoading,
  modelsError,
  reloadModels,
}: ModelsSectionProps) {
  return (
    <div className="space-y-6">
      <ModelsList
        projectId={project.id}
        models={models}
        loading={modelsLoading}
        error={modelsError}
        onChanged={reloadModels}
      />

      <SubSection title="Preview">
        <PreviewPanel projectId={project.id} models={models} />
      </SubSection>
    </div>
  )
}
