import { Link } from 'react-router-dom'
import type { ProjectDetail } from '../../types/api'
import { stagesFor, STAGE_LABELS, stageStates, type Stage, type StageState } from '../../utils/stage'

interface StageStripProps {
  project: ProjectDetail
  xttsEnabled: boolean
  /** Open + scroll to the Voice section (the Train chip target). */
  onGoToVoice: () => void
}

function ChipIndicator({ state }: { state: StageState }) {
  switch (state) {
    case 'done':
      return (
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-green-100 text-green-600 dark:bg-green-900/40 dark:text-green-400 text-xs">
          ✓
        </span>
      )
    case 'active':
      return (
        <span className="flex h-5 w-5 items-center justify-center">
          <span className="h-2.5 w-2.5 rounded-full bg-blue-500 animate-pulse" />
        </span>
      )
    case 'needs_you':
      return (
        <span className="flex h-5 w-5 items-center justify-center">
          <span className="h-2.5 w-2.5 rounded-full bg-amber-500" />
        </span>
      )
    default:
      return (
        <span className="flex h-5 w-5 items-center justify-center">
          <span className="h-2 w-2 rounded-full bg-gray-300 dark:bg-gray-600" />
        </span>
      )
  }
}

const CHIP_TEXT: Record<StageState, string> = {
  done: 'text-gray-700 dark:text-gray-300',
  active: 'text-blue-700 dark:text-blue-300 font-medium',
  needs_you: 'text-amber-700 dark:text-amber-300 font-medium',
  upcoming: 'text-gray-400 dark:text-gray-500',
}

export function StageStrip({ project, xttsEnabled, onGoToVoice }: StageStripProps) {
  const states = stageStates(project, xttsEnabled)
  const stages = stagesFor(xttsEnabled)
  const reviewClickable = project.stats.total_segments > 0

  function chip(stage: Stage) {
    const inner = (
      <span className={`flex items-center gap-1.5 text-sm ${CHIP_TEXT[states[stage]]}`}>
        <ChipIndicator state={states[stage]} />
        {STAGE_LABELS[stage]}
      </span>
    )
    if (stage === 'review' && reviewClickable) {
      return (
        <Link
          to={`/projects/${project.id}/review`}
          className="hover:opacity-75 transition-opacity"
          title="Open the review queue"
        >
          {inner}
        </Link>
      )
    }
    if (stage === 'train') {
      return (
        <button
          type="button"
          onClick={onGoToVoice}
          className="hover:opacity-75 transition-opacity"
          title="Go to the voice training section"
        >
          {inner}
        </button>
      )
    }
    return inner
  }

  return (
    <div className="flex items-center gap-2 flex-wrap" aria-label="Project stages">
      {stages.map((stage, idx) => (
        <span key={stage} className="flex items-center gap-2">
          {idx > 0 && <span className="text-gray-300 dark:text-gray-600 select-none">—</span>}
          {chip(stage)}
        </span>
      ))}
    </div>
  )
}
