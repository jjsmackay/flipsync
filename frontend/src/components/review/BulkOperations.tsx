import { useState, useEffect } from 'react'
import { bulkSegmentAction, getSegmentsCount } from '../../api/client'
import type { BulkFilter, BulkSegmentRequest, SegmentStatus } from '../../types/api'
import { ALL_SEGMENT_STATUSES } from '../../constants'

type BulkAction = BulkSegmentRequest['action']

// Mirror of the orchestrator's BULK_ACTION_SOURCES (state_machines.py): the
// segment statuses each bulk action is allowed to move FROM. The server
// intersects the filter with these; mirroring them here keeps the preview
// count equal to what Apply will actually affect.
export const BULK_ACTION_SOURCES: Record<BulkAction, readonly SegmentStatus[]> = {
  approve: ['pending', 'maybe', 'clipping_warning', 'auto_approved'],
  reject: ['pending', 'maybe', 'clipping_warning', 'approved', 'auto_approved'],
  maybe: ['pending', 'approved', 'auto_approved'],
  pending: ['maybe', 'auto_approved', 'rejected'],
}

/**
 * The statuses a bulk request will actually touch: the selected status filter
 * ('' = Any = all statuses) intersected with the action's allowed sources.
 */
export function effectiveBulkStatuses(
  action: BulkAction,
  filterStatus: SegmentStatus | '',
): SegmentStatus[] {
  const allowed = BULK_ACTION_SOURCES[action]
  const selected: readonly SegmentStatus[] = filterStatus ? [filterStatus] : ALL_SEGMENT_STATUSES
  return selected.filter((s) => allowed.includes(s))
}

interface BulkOperationsProps {
  projectId: string
  onApplied: () => void
  sources: Array<{ source_id: string; filename: string }>
}

interface Preset {
  label: string
  req: BulkSegmentRequest
}

const PRESETS: Preset[] = [
  {
    label: 'Confirm all auto-approved',
    req: { action: 'approve', filter: { status: 'auto_approved' } },
  },
  {
    label: 'Approve pending ≥0.90',
    req: { action: 'approve', filter: { status: 'pending', min_confidence: 0.9 } },
  },
  {
    label: 'Approve pending ≥0.85',
    req: { action: 'approve', filter: { status: 'pending', min_confidence: 0.85 } },
  },
  {
    label: 'Reject pending <1.5s',
    req: { action: 'reject', filter: { status: 'pending', max_duration: 1.5 } },
  },
  {
    label: 'Reject pending <2.0s',
    req: { action: 'reject', filter: { status: 'pending', max_duration: 2.0 } },
  },
  {
    label: 'Reset maybe → pending',
    req: { action: 'pending', filter: { status: 'maybe' } },
  },
]

const STATUS_VALUES: SegmentStatus[] = [
  'pending',
  'approved',
  'auto_approved',
  'rejected',
  'maybe',
  'below_threshold',
  'clipping_warning',
  'auto_rejected',
]

export function BulkOperations({ projectId, onApplied, sources }: BulkOperationsProps) {
  const [expanded, setExpanded] = useState(false)
  const [resultCount, setResultCount] = useState<number | null>(null)
  const [skippedNoTranscript, setSkippedNoTranscript] = useState(0)

  // Custom form state
  const [action, setAction] = useState<BulkSegmentRequest['action']>('approve')
  const [filterStatus, setFilterStatus] = useState<SegmentStatus | ''>('')
  const [minConfidence, setMinConfidence] = useState<string>('')
  const [minDuration, setMinDuration] = useState<string>('')
  const [maxDuration, setMaxDuration] = useState<string>('')
  const [sourceId, setSourceId] = useState<string>('')
  const [previewCount, setPreviewCount] = useState<number | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applyingPreset, setApplyingPreset] = useState<number | null>(null)
  const [bulkError, setBulkError] = useState<string | null>(null)

  // Statuses this action can actually affect, given the selected status filter.
  // Empty means the combination is a no-op (e.g. Approve over rejected segments).
  const effectiveStatuses = effectiveBulkStatuses(action, filterStatus)

  function buildFilter(): BulkFilter {
    const f: BulkFilter = {}
    // Send the explicit intersected status list so the preview count matches what
    // Apply affects — an empty status would fall back to the server's
    // pending+maybe default (SC4), and un-intersected statuses would overstate.
    f.status = effectiveStatuses.join(',')
    if (minConfidence !== '') f.min_confidence = parseFloat(minConfidence)
    if (minDuration !== '') f.min_duration = parseFloat(minDuration)
    if (maxDuration !== '') f.max_duration = parseFloat(maxDuration)
    if (sourceId) f.source_id = sourceId
    return f
  }

  // Live preview: recount whenever the panel is open and any filter changes.
  useEffect(() => {
    if (!expanded) return
    if (effectiveStatuses.length === 0) {
      // Nothing this action can touch — skip the count round-trip.
      setPreviewCount(0)
      setPreviewing(false)
      setBulkError(null)
      return
    }
    const filter = buildFilter()
    let cancelled = false
    setPreviewing(true)
    const timer = setTimeout(() => {
      getSegmentsCount(projectId, filter)
        .then((result) => {
          if (!cancelled) {
            setPreviewCount(result.total)
            setBulkError(null)
          }
        })
        .catch((err) => {
          if (!cancelled) {
            setPreviewCount(null)
            setBulkError(err instanceof Error ? err.message : 'Preview failed')
          }
        })
        .finally(() => {
          if (!cancelled) setPreviewing(false)
        })
    }, 300)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded, projectId, action, filterStatus, minConfidence, minDuration, maxDuration, sourceId])

  async function handlePreset(index: number) {
    setApplyingPreset(index)
    setBulkError(null)
    try {
      const result = await bulkSegmentAction(projectId, PRESETS[index].req)
      setResultCount(result.affected_count)
      setSkippedNoTranscript(result.skipped_no_transcript ?? 0)
      onApplied()
    } catch (err) {
      setBulkError(err instanceof Error ? err.message : 'Bulk action failed')
    } finally {
      setApplyingPreset(null)
    }
  }

  async function handleApply() {
    if (previewCount === null || previewCount === 0) return
    setApplying(true)
    setBulkError(null)
    try {
      const req: BulkSegmentRequest = { action, filter: buildFilter() }
      const result = await bulkSegmentAction(projectId, req)
      setResultCount(result.affected_count)
      setSkippedNoTranscript(result.skipped_no_transcript ?? 0)
      onApplied()
    } catch (err) {
      setBulkError(err instanceof Error ? err.message : 'Bulk action failed')
    } finally {
      setApplying(false)
    }
  }

  return (
    <div className="border border-slate-200 dark:border-slate-700 rounded-lg bg-white dark:bg-gray-800">
      <button
        onClick={() => setExpanded(e => !e)}
        className="w-full flex items-center justify-between px-4 py-2 text-sm font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700/50 rounded-lg"
      >
        <span>Bulk operations</span>
        <span className="text-xs text-slate-400 dark:text-slate-500">{expanded ? '▲' : '▼'}</span>
      </button>

      {expanded && (
        <div className="px-4 pb-4 space-y-4">
          {resultCount !== null && (
            <div className="rounded bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 text-green-800 dark:text-green-300 text-sm px-3 py-2">
              Applied — {resultCount} segment{resultCount !== 1 ? 's' : ''} affected.
              {skippedNoTranscript > 0 && (
                <span className="block text-amber-700 dark:text-amber-400 mt-0.5">
                  {skippedNoTranscript} skipped — no transcript. Transcribe them before approving.
                </span>
              )}
            </div>
          )}

          {/* Presets */}
          <div>
            <p className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wide mb-2">Presets</p>
            <div className="flex flex-wrap gap-2">
              {PRESETS.map((preset, i) => (
                <button
                  key={i}
                  onClick={() => handlePreset(i)}
                  disabled={applyingPreset !== null}
                  className="text-xs px-3 py-1.5 rounded border border-slate-300 dark:border-slate-600 bg-slate-50 dark:bg-slate-700 hover:bg-slate-100 dark:hover:bg-slate-600 disabled:opacity-50 disabled:cursor-not-allowed text-slate-700 dark:text-slate-200"
                >
                  {applyingPreset === i ? 'Applying…' : preset.label}
                </button>
              ))}
            </div>
          </div>

          <hr className="border-slate-200 dark:border-slate-700" />

          {/* Custom */}
          <div>
            <p className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wide mb-2">Custom</p>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <div>
                <label className="block text-xs text-slate-600 dark:text-slate-400 mb-1">Action</label>
                <select
                  value={action}
                  onChange={e => setAction(e.target.value as BulkSegmentRequest['action'])}
                  className="w-full text-sm border border-slate-300 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 rounded px-2 py-1"
                >
                  <option value="approve">Approve</option>
                  <option value="reject">Reject</option>
                  <option value="maybe">Maybe</option>
                  <option value="pending">Pending</option>
                </select>
              </div>

              <div>
                <label className="block text-xs text-slate-600 dark:text-slate-400 mb-1">Status filter</label>
                <select
                  value={filterStatus}
                  onChange={e => setFilterStatus(e.target.value as SegmentStatus | '')}
                  className="w-full text-sm border border-slate-300 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 rounded px-2 py-1"
                >
                  <option value="">Any</option>
                  {STATUS_VALUES.map(s => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-xs text-slate-600 dark:text-slate-400 mb-1">Min confidence</label>
                <input
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={minConfidence}
                  onChange={e => setMinConfidence(e.target.value)}
                  placeholder="e.g. 0.85"
                  className="w-full text-sm border border-slate-300 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 rounded px-2 py-1"
                />
              </div>

              <div>
                <label className="block text-xs text-slate-600 dark:text-slate-400 mb-1">Min duration (s)</label>
                <input
                  type="number"
                  min={0}
                  step={0.5}
                  value={minDuration}
                  onChange={e => setMinDuration(e.target.value)}
                  placeholder="e.g. 2.0"
                  className="w-full text-sm border border-slate-300 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 rounded px-2 py-1"
                />
              </div>

              <div>
                <label className="block text-xs text-slate-600 dark:text-slate-400 mb-1">Max duration (s)</label>
                <input
                  type="number"
                  min={0}
                  step={0.5}
                  value={maxDuration}
                  onChange={e => setMaxDuration(e.target.value)}
                  placeholder="e.g. 2.0"
                  className="w-full text-sm border border-slate-300 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 rounded px-2 py-1"
                />
              </div>

              {sources.length > 1 && (
                <div>
                  <label className="block text-xs text-slate-600 dark:text-slate-400 mb-1">Source</label>
                  <select
                    value={sourceId}
                    onChange={e => setSourceId(e.target.value)}
                    className="w-full text-sm border border-slate-300 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 rounded px-2 py-1"
                  >
                    <option value="">All sources</option>
                    {sources.map(s => (
                      <option key={s.source_id} value={s.source_id}>{s.filename}</option>
                    ))}
                  </select>
                </div>
              )}
            </div>

            <div className="flex items-center gap-3 mt-3">
              <span className="text-sm text-slate-600 dark:text-slate-400">
                {previewing
                  ? 'Counting…'
                  : previewCount !== null
                    ? `Affects ${previewCount} segment${previewCount !== 1 ? 's' : ''}`
                    : '—'}
              </span>

              {effectiveStatuses.length === 0 && filterStatus && (
                <span className="text-xs text-amber-700 dark:text-amber-400">
                  {action.charAt(0).toUpperCase() + action.slice(1)} doesn't apply to {filterStatus} segments
                </span>
              )}

              <button
                onClick={handleApply}
                disabled={applying || previewing || previewCount === null || previewCount === 0 || effectiveStatuses.length === 0}
                className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed ml-auto"
              >
                {applying ? 'Applying…' : 'Apply'}
              </button>
            </div>

            {bulkError && (
              <p className="mt-2 text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded px-2 py-1">
                {bulkError}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
