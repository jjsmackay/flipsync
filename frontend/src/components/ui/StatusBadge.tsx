import type { ModelStatus, ProjectStatus, SegmentStatus, SourceStatus } from '../../types/api'
import { modelStatusLabel, statusLabel } from '../../utils/labels'

type AnyStatus = ProjectStatus | SegmentStatus | SourceStatus | ModelStatus

const STATUS_STYLES: Record<string, string> = {
  new: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  processing: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  awaiting_reference: 'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300',
  review: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  ready: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  exporting: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  exported: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  complete: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  pending: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  approved: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  rejected: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  maybe: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  below_threshold: 'bg-gray-200 text-gray-500 dark:bg-gray-700 dark:text-gray-400',
  clipping_warning: 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
  auto_rejected: 'bg-red-200 text-red-600 dark:bg-red-900/50 dark:text-red-300',
  auto_approved: 'bg-teal-100 text-teal-700 dark:bg-teal-900/40 dark:text-teal-300',
  uploaded: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
  extracting: 'bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300',
  separation_pending: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
  separation_running: 'bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300',
  separation_failed: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  diarisation_pending: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
  diarisation_running: 'bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300',
  diarisation_failed: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  extraction_failed: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
}

// Model statuses collide with project/segment statuses that mean something
// different ('ready' project = grey not-started; 'ready' model = green trained;
// 'pending' segment = unreviewed; 'pending' model = queued), so they resolve
// through a scoped map selected by kind="model" instead of STATUS_STYLES.
const MODEL_STATUS_STYLES: Record<ModelStatus, string> = {
  pending: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  training: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  ready: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  failed: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  cancelled: 'bg-gray-200 text-gray-500 dark:bg-gray-700 dark:text-gray-400',
}

const STATUS_DOT: Record<string, string> = {
  approved: 'bg-green-500',
  rejected: 'bg-red-500',
  maybe: 'bg-yellow-500',
  pending: 'bg-gray-400',
  below_threshold: 'bg-gray-300',
  clipping_warning: 'bg-orange-400',
  auto_rejected: 'bg-red-400',
  auto_approved: 'bg-teal-500',
}

interface StatusBadgeProps {
  status: AnyStatus
  dot?: boolean
  /** Scope the style/label lookup for statuses whose names collide across domains. */
  kind?: 'model'
}

export function StatusBadge({ status, dot = false, kind }: StatusBadgeProps) {
  const style =
    (kind === 'model' ? MODEL_STATUS_STYLES[status as ModelStatus] : STATUS_STYLES[status]) ??
    'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
  const label = kind === 'model' ? modelStatusLabel(status as ModelStatus) : statusLabel(status)
  const dotStyle = STATUS_DOT[status]

  if (dot && dotStyle) {
    return (
      <span
        className={`inline-block w-2.5 h-2.5 rounded-full ${dotStyle}`}
        title={label}
      />
    )
  }

  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${style}`}>
      {label}
    </span>
  )
}
