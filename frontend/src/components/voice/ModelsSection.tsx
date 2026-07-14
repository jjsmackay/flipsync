import type { ProjectDetail, Model } from '../../types/api'
import { ModelsList } from './ModelsList'
import { PreviewPanel } from './PreviewPanel'
import { ComparePanel } from './ComparePanel'

interface ModelsSectionProps {
  project: ProjectDetail
  /** Header toggle: forwarded to the preview sampling dials. */
  advanced?: boolean
  // Models state is owned by the dashboard (the pipeline's Train row needs it
  // too); this section renders it and asks for reloads. Training itself lives
  // on the pipeline's Train row — this section is trained models + preview.
  models: Model[]
  modelsLoading: boolean
  modelsError: string | null
  reloadModels: () => void
  /** Whether the deployment's XTTS engine is healthy — forwarded to the
   *  Preview panel to gate its zero-shot base-model column (GPT-SoVITS has
   *  no untrained preview). */
  xttsAvailable?: boolean
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
  advanced = false,
  models,
  modelsLoading,
  modelsError,
  reloadModels,
  xttsAvailable = true,
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
        <PreviewPanel
          projectId={project.id}
          models={models}
          advanced={advanced}
          xttsAvailable={xttsAvailable}
        />
      </SubSection>

      <SubSection title="Compare">
        <ComparePanel projectId={project.id} models={models} advanced={advanced} />
      </SubSection>
    </div>
  )
}
