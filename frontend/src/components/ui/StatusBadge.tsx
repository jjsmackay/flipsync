import type { ProjectStatus, SegmentStatus, SourceStatus } from '../../types/api'

type AnyStatus = ProjectStatus | SegmentStatus | SourceStatus

const STATUS_STYLES: Record<string, string> = {
  new: 'bg-gray-100 text-gray-700',
  processing: 'bg-blue-100 text-blue-700',
  review: 'bg-yellow-100 text-yellow-700',
  complete: 'bg-green-100 text-green-700',
  pending: 'bg-gray-100 text-gray-700',
  approved: 'bg-green-100 text-green-700',
  rejected: 'bg-red-100 text-red-700',
  maybe: 'bg-yellow-100 text-yellow-700',
  below_threshold: 'bg-gray-200 text-gray-500',
  clipping_warning: 'bg-orange-100 text-orange-700',
  auto_rejected: 'bg-red-200 text-red-600',
  uploading: 'bg-blue-100 text-blue-600',
  extracting: 'bg-blue-100 text-blue-600',
  step1_pending: 'bg-gray-100 text-gray-600',
  step1_running: 'bg-blue-100 text-blue-600',
  step1_failed: 'bg-red-100 text-red-700',
  step2_pending: 'bg-gray-100 text-gray-600',
  step2_running: 'bg-blue-100 text-blue-600',
  step2_failed: 'bg-red-100 text-red-700',
  extraction_failed: 'bg-red-100 text-red-700',
}

const STATUS_DOT: Record<string, string> = {
  approved: 'bg-green-500',
  rejected: 'bg-red-500',
  maybe: 'bg-yellow-500',
  pending: 'bg-gray-400',
  below_threshold: 'bg-gray-300',
  clipping_warning: 'bg-orange-400',
  auto_rejected: 'bg-red-400',
}

interface StatusBadgeProps {
  status: AnyStatus
  dot?: boolean
}

export function StatusBadge({ status, dot = false }: StatusBadgeProps) {
  const style = STATUS_STYLES[status] ?? 'bg-gray-100 text-gray-600'
  const dotStyle = STATUS_DOT[status]

  if (dot && dotStyle) {
    return (
      <span
        className={`inline-block w-2.5 h-2.5 rounded-full ${dotStyle}`}
        title={status.replace(/_/g, ' ')}
      />
    )
  }

  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${style}`}>
      {status.replace(/_/g, ' ')}
    </span>
  )
}
