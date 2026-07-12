import type { FilterState } from '../../hooks/useFilterState'
import type { SourceCoverage } from '../../types/api'
import { ALL_SEGMENT_STATUSES_CSV } from '../../constants'

interface FilterBarProps {
  filter: FilterState
  sources: SourceCoverage[]
  onChange: (update: Partial<FilterState>) => void
}

const STATUS_OPTIONS = [
  // "All" must be an explicit status list, not an empty value — otherwise the server
  // falls back to its pending+maybe default (SC4).
  { value: ALL_SEGMENT_STATUSES_CSV, label: 'All' },
  { value: 'pending,maybe', label: 'Pending + Maybe' },
  { value: 'pending', label: 'Pending' },
  { value: 'maybe', label: 'Maybe' },
  { value: 'approved', label: 'Approved' },
  { value: 'auto_approved', label: 'Auto-approved' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'clipping_warning', label: 'Clipping warning' },
  { value: 'below_threshold', label: 'Below threshold' },
  { value: 'auto_rejected', label: 'Auto-rejected' },
]

const SORT_OPTIONS = [
  { value: 'match_confidence', label: 'Confidence' },
  { value: 'duration', label: 'Duration' },
  { value: 'start_secs', label: 'Source order' },
  { value: 'transcript_confidence', label: 'Transcript confidence' },
  { value: 'uncertainty', label: 'Uncertainty (most borderline first)' },
]

// The API defaults order=asc for uncertainty (most-borderline-first) and desc for
// everything else. Switching the sort field resets order to that sort's sensible
// default; the order toggle button still lets the user flip it afterwards.
const DEFAULT_ORDER_FOR_SORT: Record<string, 'asc' | 'desc'> = {
  uncertainty: 'asc',
}

export function FilterBar({ filter, sources, onChange }: FilterBarProps) {
  return (
    <div className="flex flex-wrap gap-3 items-center py-2 px-4 bg-white border-b border-gray-200">
      {/* Status */}
      <label className="flex items-center gap-1.5 text-sm text-gray-600">
        Status
        <select
          className="border border-gray-300 rounded px-2 py-1 text-sm text-gray-800 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-400"
          value={filter.status}
          onChange={e => onChange({ status: e.target.value })}
        >
          {STATUS_OPTIONS.map(o => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>

      {/* Source — only show if multiple sources */}
      {sources.length > 1 && (
        <label className="flex items-center gap-1.5 text-sm text-gray-600">
          Source
          <select
            className="border border-gray-300 rounded px-2 py-1 text-sm text-gray-800 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-400 max-w-[180px] truncate"
            value={filter.source_id}
            onChange={e => onChange({ source_id: e.target.value })}
          >
            <option value="">All sources</option>
            {sources.map(s => (
              <option key={s.source_id} value={s.source_id}>
                {s.filename}
              </option>
            ))}
          </select>
        </label>
      )}

      {/* Min confidence */}
      <label className="flex items-center gap-1.5 text-sm text-gray-600">
        Min confidence
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={filter.min_confidence}
          onChange={e => onChange({ min_confidence: parseFloat(e.target.value) })}
          className="w-24 accent-indigo-600"
        />
        <span className="font-mono text-xs text-gray-700 w-8 text-right">
          {(filter.min_confidence * 100).toFixed(0)}%
        </span>
      </label>

      {/* Min duration */}
      <label className="flex items-center gap-1.5 text-sm text-gray-600">
        Min duration
        <input
          type="number"
          min={0}
          step={0.5}
          value={filter.min_duration || ''}
          placeholder="0"
          onChange={e => onChange({ min_duration: parseFloat(e.target.value) || 0 })}
          className="border border-gray-300 rounded px-2 py-1 text-sm w-16 text-gray-800 focus:outline-none focus:ring-2 focus:ring-indigo-400"
        />
        <span className="text-xs text-gray-500">s</span>
      </label>

      {/* Sort */}
      <label className="flex items-center gap-1.5 text-sm text-gray-600">
        Sort
        <select
          className="border border-gray-300 rounded px-2 py-1 text-sm text-gray-800 bg-white focus:outline-none focus:ring-2 focus:ring-indigo-400"
          value={filter.sort}
          onChange={e => {
            const sort = e.target.value
            onChange({ sort, order: DEFAULT_ORDER_FOR_SORT[sort] ?? 'desc' })
          }}
        >
          {SORT_OPTIONS.map(o => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>

      {/* Order toggle */}
      <button
        type="button"
        onClick={() => onChange({ order: filter.order === 'asc' ? 'desc' : 'asc' })}
        className="border border-gray-300 rounded px-2 py-1 text-sm bg-white hover:bg-gray-50 text-gray-700 focus:outline-none focus:ring-2 focus:ring-indigo-400"
        title={filter.order === 'asc' ? 'Ascending — click for descending' : 'Descending — click for ascending'}
      >
        {filter.order === 'asc' ? '↑' : '↓'}
      </button>
    </div>
  )
}
