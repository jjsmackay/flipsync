import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useProjectPolling } from '../hooks/useProjectPolling'
import {
  reprocessSource,
  runTranscription,
  triggerExport,
  startScout,
  getCapabilities,
  getModels,
  ApiError,
} from '../api/client'
import type { EngineInfo, FailedJob, Model } from '../types/api'
import { retryPlan } from '../utils/retry'
import { errorMessage } from '../utils/errors'
import { deriveStage } from '../utils/stage'
import { FailedJobsPanel } from '../components/project/FailedJobsPanel'
import { StageStrip } from '../components/project/StageStrip'
import { NextActionCard } from '../components/project/NextActionCard'
import { ReferenceCard } from '../components/project/ReferenceCard'
import { SourcesTable } from '../components/project/SourcesTable'
import { PipelineSteps } from '../components/project/PipelineSteps'
import { CompareSettingsModal } from '../components/project/CompareSettingsModal'
import { UploadArea } from '../components/project/UploadArea'
import { ThemeToggle } from '../components/ui/ThemeToggle'
import { CollapsibleSection, type CollapsibleSectionHandle } from '../components/ui/CollapsibleSection'
import { ModelsSection } from '../components/voice/ModelsSection'

// The header's Advanced toggle: a per-browser viewing preference (like the
// section-collapse memory), NOT project config — it changes which knobs render,
// never what any run does. localStorage can throw (private mode) — degrade.
const ADVANCED_KEY = 'flipsync:ui:advanced'

function readAdvancedPref(): boolean {
  try {
    return localStorage.getItem(ADVANCED_KEY) === '1'
  } catch {
    return false
  }
}

function writeAdvancedPref(on: boolean): void {
  try {
    localStorage.setItem(ADVANCED_KEY, on ? '1' : '0')
  } catch {
    /* ignore */
  }
}

// Voice-engine capabilities are a deployment fact, fetched once. Memoise a
// resolved "training enabled" state at module scope so a later transient
// probe failure can't flip an enabled deployment back to the Export terminal
// stage. `voice_training` (not the back-compat-only `xtts` flag) drives this —
// a GPT-SoVITS-only deployment (xtts absent/unhealthy, gpt_sovits healthy)
// must offer the Train stage too.
let voiceCapabilitiesResolved: { voiceTrainingEnabled: boolean; engines: EngineInfo[] } | null = null

function useVoiceCapabilities(): { voiceTrainingEnabled: boolean; engines: EngineInfo[] } {
  const [state, setState] = useState(
    voiceCapabilitiesResolved ?? { voiceTrainingEnabled: false, engines: [] },
  )
  useEffect(() => {
    if (voiceCapabilitiesResolved) return
    let alive = true
    getCapabilities()
      .then((caps) => {
        if (alive && caps.voice_training) {
          voiceCapabilitiesResolved = { voiceTrainingEnabled: true, engines: caps.engines ?? [] }
          setState(voiceCapabilitiesResolved)
        }
      })
      .catch(() => {
        /* treat an unreachable/failed probe as voice training disabled */
      })
    return () => {
      alive = false
    }
  }, [])
  return state
}

interface ReprocessConfirm {
  // One id for the per-source kebab path; several when a step row re-runs all
  // eligible sources and any of them would invalidate approvals.
  sourceIds: string[]
  steps: string[]
  message: string
}

// Sources a step re-run may target: terminal for that step (mid-pipeline
// sources are skipped rather than erroring one by one). Mirrors the enable
// rule in PipelineSteps.
const RERUN_ELIGIBLE: Record<'separation' | 'diarisation', string[]> = {
  separation: ['complete', 'separation_failed', 'diarisation_failed'],
  diarisation: ['complete', 'diarisation_failed'],
}

export function ProjectDashboardPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const { project, isLoading, error, refetch } = useProjectPolling(projectId!)

  const [reprocessError, setReprocessError] = useState<string | null>(null)
  const [reprocessConfirm, setReprocessConfirm] = useState<ReprocessConfirm | null>(null)
  const [retryingJobId, setRetryingJobId] = useState<string | null>(null)
  const [compareOpen, setCompareOpen] = useState(false)
  const [advanced, setAdvanced] = useState(readAdvancedPref)
  const { voiceTrainingEnabled, engines } = useVoiceCapabilities()
  const xttsAvailable = engines.some((e) => e.id === 'xtts' && e.healthy)

  function toggleAdvanced(on: boolean) {
    setAdvanced(on)
    writeAdvancedPref(on)
  }
  const pipelineRef = useRef<CollapsibleSectionHandle>(null)
  const reviewSettingsRef = useRef<HTMLDetailsElement>(null)
  const trainRowRef = useRef<HTMLDivElement>(null)
  const modelsRef = useRef<CollapsibleSectionHandle>(null)

  // Models state lives here (not in VoiceSection): the pipeline's Train row
  // needs it for its chip too. Reload on mount and around voice-job edges —
  // model rows change status (pending → training → ready|failed) there.
  const [models, setModels] = useState<Model[]>([])
  const [modelsLoading, setModelsLoading] = useState(true)
  const [modelsError, setModelsError] = useState<string | null>(null)
  const reloadModels = useCallback(async () => {
    if (!projectId) return
    try {
      const res = await getModels(projectId)
      setModels(res.models)
      setModelsError(null)
    } catch (err) {
      setModelsError(err instanceof Error ? err.message : 'Failed to load models.')
    } finally {
      setModelsLoading(false)
    }
  }, [projectId])
  const voiceJobActive = Boolean(
    project?.active_jobs.some((j) => j.type === 'finetune' || j.type === 'dataset_build'),
  )
  useEffect(() => {
    void reloadModels()
  }, [reloadModels, voiceJobActive])

  function openAndScroll(handle: CollapsibleSectionHandle | null) {
    handle?.open()
    // Wait for the section to expand before scrolling to it.
    requestAnimationFrame(() => handle?.el?.scrollIntoView({ behavior: 'smooth' }))
  }

  // Review settings live in the Review step row's disclosure — open the
  // Pipeline section, then expand + scroll to the disclosure.
  function openSettings() {
    pipelineRef.current?.open()
    requestAnimationFrame(() => {
      const el = reviewSettingsRef.current
      if (!el) return
      el.open = true
      el.scrollIntoView({ behavior: 'smooth' })
    })
  }

  function goToModels() {
    openAndScroll(modelsRef.current)
  }

  // Training lives on the pipeline's Train row — open the Pipeline section,
  // then scroll to that row.
  function goToTrain() {
    pipelineRef.current?.open()
    requestAnimationFrame(() => trainRowRef.current?.scrollIntoView({ behavior: 'smooth' }))
  }

  // Target of the strip's pipeline chips (Separate/Match/Transcribe).
  function goToPipeline() {
    openAndScroll(pipelineRef.current)
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
        setReprocessConfirm({ sourceIds: [sourceId], steps, message: err.message })
      } else {
        setReprocessError(errorMessage(err, 'Reprocess failed'))
      }
    }
  }

  // Step-row re-run: submit every eligible source; if any would invalidate
  // approvals, surface ONE confirm covering the ones that 409ed.
  async function handleReprocessAll(steps: string[]) {
    if (!project) return
    setReprocessError(null)
    const eligible = RERUN_ELIGIBLE[steps.includes('separation') ? 'separation' : 'diarisation']
    const targets = project.stats.source_coverage.filter((s) => eligible.includes(s.status))
    const needConfirm: string[] = []
    let message = ''
    for (const src of targets) {
      try {
        await submitReprocess(src.source_id, steps, false)
      } catch (err) {
        if (err instanceof ApiError && err.error === 'would_invalidate_approvals') {
          needConfirm.push(src.source_id)
          message = err.message
        } else {
          setReprocessError(err instanceof Error ? err.message : 'Reprocess failed')
          return
        }
      }
    }
    if (needConfirm.length > 0) {
      setReprocessConfirm({ sourceIds: needConfirm, steps, message })
    }
  }

  async function handleConfirmReprocess() {
    if (!reprocessConfirm) return
    const { sourceIds, steps } = reprocessConfirm
    setReprocessConfirm(null)
    setReprocessError(null)
    try {
      for (const sourceId of sourceIds) {
        await submitReprocess(sourceId, steps, true)
      }
    } catch (err) {
      setReprocessError(errorMessage(err, 'Reprocess failed'))
    }
  }

  async function handleRunTranscription() {
    if (!projectId) return
    setReprocessError(null)
    try {
      await runTranscription(projectId)
      void refetch()
    } catch (err) {
      setReprocessError(err instanceof Error ? err.message : 'Failed to start transcription')
    }
  }

  async function handleRetryJob(job: FailedJob) {
    const plan = retryPlan(job)
    if (!projectId || !plan) return
    setReprocessError(null)
    setRetryingJobId(job.id)
    try {
      if (plan.kind === 'transcription') {
        await runTranscription(projectId)
      } else if (plan.kind === 'export') {
        await triggerExport(projectId)
      } else if (plan.kind === 'scout') {
        await startScout(projectId, plan.sourceId)
      } else {
        // Reprocess retries go through the same confirm flow as a manual
        // reprocess: submit without confirm, surface the invalidation dialog
        // on 409 rather than silently wiping approvals.
        try {
          await submitReprocess(plan.sourceId, plan.steps, false)
        } catch (err) {
          if (err instanceof ApiError && err.error === 'would_invalidate_approvals') {
            setReprocessConfirm({ sourceIds: [plan.sourceId], steps: plan.steps, message: err.message })
            return
          }
          throw err
        }
      }
      void refetch()
    } catch (err) {
      setReprocessError(errorMessage(err, 'Retry failed'))
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

  // Smart-default open state: Sources and Pipeline are always default-open
  // (Pipeline hosts the whole journey including Review and Train); Models
  // opens by default once the project reaches the Train stage.
  // CollapsibleSection lets an explicit user toggle override and persist this.
  const stage = deriveStage(project, voiceTrainingEnabled)
  const modelsDefaultOpen = stage === 'train'

  return (
    <div className="max-w-4xl mx-auto px-6 py-8 space-y-8">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <Link
            to="/"
            className="inline-block mb-1 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 text-sm"
          >
            ← Projects
          </Link>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 truncate">
            {project.name}
          </h1>
        </div>
        <div className="flex items-center gap-3 flex-shrink-0">
          <label
            className="flex items-center gap-1.5 text-xs font-medium text-gray-500 dark:text-gray-400 cursor-pointer select-none"
            title="Show advanced tuning settings (GPU, sampling, and DSP internals)"
          >
            <input
              type="checkbox"
              checked={advanced}
              onChange={(e) => toggleAdvanced(e.target.checked)}
              className="accent-blue-600 w-3.5 h-3.5"
            />
            Advanced
          </label>
          <ThemeToggle />
          {hasSegments && (
            <Link
              to={`/projects/${project.id}/qc`}
              title="Play approved segments before and after cleanup"
              className="px-2.5 py-1.5 text-xs font-medium text-gray-600 dark:text-gray-300 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
            >
              Cleaned QC
            </Link>
          )}
          {hasSegments && (
            <button
              onClick={openSettings}
              aria-label="Project settings"
              title="Project settings"
              className="p-2 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
            >
              ⚙
            </button>
          )}
        </div>
      </div>

      {/* Stage strip */}
      <StageStrip
        project={project}
        voiceTrainingEnabled={voiceTrainingEnabled}
        onGoToTrain={goToTrain}
        onGoToPipeline={goToPipeline}
      />

      {/* Next action */}
      <NextActionCard
        project={project}
        onAction={() => void refetch()}
        onOpenSettings={openSettings}
        voiceTrainingEnabled={voiceTrainingEnabled}
        onGoToTrain={goToTrain}
      />

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
        <CollapsibleSection title="Sources" sectionKey="sources" defaultOpen>
          <div className="space-y-2">
            {project.reference_path && (
              <ReferenceCard project={project} onAction={() => void refetch()} />
            )}
            <SourcesTable
              sources={project.stats.source_coverage}
              onReprocess={handleReprocess}
            />
            <UploadArea projectId={project.id} onUploaded={() => void refetch()} compact />
            {reprocessError && (
              <p className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded px-3 py-2">
                {reprocessError}
              </p>
            )}
          </div>
        </CollapsibleSection>
      )}

      {/* Pipeline — the six-step stepper */}
      {hasSources && (
        <CollapsibleSection
          ref={pipelineRef}
          title="Pipeline"
          sectionKey="pipeline"
          defaultOpen
        >
          <PipelineSteps
            project={project}
            voiceTrainingEnabled={voiceTrainingEnabled}
            engines={engines}
            onSaved={() => void refetch()}
            onReprocessAll={(steps) => void handleReprocessAll(steps)}
            onRunTranscription={() => void handleRunTranscription()}
            onOpenCompare={() => setCompareOpen(true)}
            reviewSettingsRef={reviewSettingsRef}
            models={models}
            onGoToModels={goToModels}
            onTrainStarted={() => {
              void refetch()
              void reloadModels()
            }}
            trainRowRef={trainRowRef}
            advanced={advanced}
          />
        </CollapsibleSection>
      )}

      {/* Models (voice-training deployments) — trained models + voice preview */}
      {voiceTrainingEnabled && hasSegments && (
        <CollapsibleSection
          ref={modelsRef}
          title="Models"
          sectionKey="models"
          defaultOpen={modelsDefaultOpen}
        >
          <ModelsSection
            project={project}
            models={models}
            modelsLoading={modelsLoading}
            modelsError={modelsError}
            reloadModels={() => void reloadModels()}
            advanced={advanced}
            xttsAvailable={xttsAvailable}
          />
        </CollapsibleSection>
      )}

      {/* Cleanup settings A/B compare */}
      {compareOpen && (
        <CompareSettingsModal
          projectId={project.id}
          config={project.config}
          onSaved={() => void refetch()}
          onClose={() => setCompareOpen(false)}
        />
      )}

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
