import type { Segment } from '../../types/api'
import { ConfidenceBadge } from '../ui/ConfidenceBadge'
import { StatusBadge } from '../ui/StatusBadge'
import { formatSecondsPrecise } from '../../utils/format'

interface SegmentCardProps {
  segment: Segment
  selected: boolean
  onClick: () => void
}

export function SegmentCard({ segment, selected, onClick }: SegmentCardProps) {
  const displayTranscript = segment.transcript_edited ?? segment.transcript

  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'w-full text-left px-3 py-2.5 bg-white dark:bg-gray-800 border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors',
        'flex flex-col gap-1 focus:outline-none focus:bg-indigo-50 dark:focus:bg-indigo-900/30',
        selected ? 'border-l-4 border-l-indigo-500 pl-2' : 'border-l-4 border-l-transparent',
      ].join(' ')}
    >
      {/* Top row: badges, duration, status dot, clipping */}
      <div className="flex items-center gap-1.5 flex-wrap">
        <ConfidenceBadge value={segment.match_confidence} />
        <span className="text-xs text-gray-500 dark:text-gray-400 font-mono">{formatSecondsPrecise(segment.duration_secs)}</span>
        <StatusBadge status={segment.status} dot />
        {segment.clipping_warning && (
          <span title="Clipping warning" className="text-orange-500 text-xs">
            ⚡
          </span>
        )}
      </div>

      {/* Transcript preview */}
      <p
        className="text-sm text-gray-700 dark:text-gray-300 leading-snug overflow-hidden"
        style={{
          display: '-webkit-box',
          WebkitLineClamp: 2,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
        }}
      >
        {displayTranscript ?? <span className="italic text-gray-400 dark:text-gray-500">No transcript</span>}
      </p>

      {/* Source filename */}
      <p className="text-xs text-gray-400 dark:text-gray-500 truncate">{segment.source_filename}</p>
    </button>
  )
}
