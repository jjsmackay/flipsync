import { useState } from 'react'
import { bulkSegmentAction, getSegmentsCount } from '../../api/client'
import type { BulkFilter, BulkSegmentRequest, SegmentStatus } from '../../types/api'

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
  'rejected',
  'maybe',
  'below_threshold',
  'clipping_warning',
  'auto_rejected',
]

export function BulkOperations({ projectId, onApplied, sources }: BulkOperationsProps) {
  const [expanded, setExpanded] = useState(false)
  const [resultCount, setResultCount] = useState<number | null>(null)

  // Custom form state
  const [action, setAction] = useState<BulkSegmentRequest['action']>('approve')
  const [filterStatus, setFilterStatus] = useState<SegmentStatus | ''>('')
  const [minConfidence, setMinConfidence] = useState<string>('')
  const [maxDuration, setMaxDuration] = useState<string>('')
  const [sourceId, setSourceId] = useState<string>('')
  const [previewCount, setPreviewCount] = useState<number | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applyingPreset, setApplyingPreset] = useState<number | null>(null)

  function buildFilter(): BulkFilter {
    const f: BulkFilter = {}
    if (filterStatus) f.status = filterStatus
    if (minConfidence !== '') f.min_confidence = parseFloat(minConfidence)
    if (maxDuration !== '') f.max_duration = parseFloat(maxDuration)
    if (sourceId) f.source_id = sourceId
    return f
  }

  async function handlePreset(index: number) {
    setApplyingPreset(index)
    try {
      const result = await bulkSegmentAction(projectId, PRESETS[index].req)
      setResultCount(result.affected_count)
      onApplied()
    } finally {
      setApplyingPreset(null)
    }
  }

  async function handlePreview() {
    setPreviewing(true)
    try {
      const filter = buildFilter()
      const result = await getSegmentsCount(projectId, filter)
      setPreviewCount(result.total)
    } finally {
      setPreviewing(false)
    }
  }

  async function handleApply() {
    if (previewCount === null || previewCount === 0) return
    setApplying(true)
    try {
      const req: BulkSegmentRequest = { action, filter: buildFilter() }
      const result = await bulkSegmentAction(projectId, req)
      setResultCount(result.affected_count)
      setPreviewCount(null)
      onApplied()
    } finally {
      setApplying(false)
    }
  }

  return (
    <div className="border border-slate-200 rounded-lg bg-white">
      <button
        onClick={() => setExpanded(e => !e)}
        className="w-full flex items-center justify-between px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 rounded-lg"
      >
        <span>Bulk operations</span>
        <span className="text-xs text-slate-400">{expanded ? '▲' : '▼'}</span>
      </button>

      {expanded && (
        <div className="px-4 pb-4 space-y-4">
          {resultCount !== null && (
            <div className="rounded bg-green-50 border border-green-200 text-green-800 text-sm px-3 py-2">
              Applied — {resultCount} segment{resultCount !== 1 ? 's' : ''} affected.
            </div>
          )}

          {/* Presets */}
          <div>
            <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Presets</p>
            <div className="flex flex-wrap gap-2">
              {PRESETS.map((preset, i) => (
                <button
                  key={i}
                  onClick={() => handlePreset(i)}
                  disabled={applyingPreset !== null}
                  className="text-xs px-3 py-1.5 rounded border border-slate-300 bg-slate-50 hover:bg-slate-100 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {applyingPreset === i ? 'Applying…' : preset.label}
                </button>
              ))}
            </div>
          </div>

          <hr className="border-slate-200" />

          {/* Custom */}
          <div>
            <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Custom</p>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <div>
                <label className="block text-xs text-slate-600 mb-1">Action</label>
                <select
                  value={action}
                  onChange={e => {
                    setAction(e.target.value as BulkSegmentRequest['action'])
                    setPreviewCount(null)
                  }}
                  className="w-full text-sm border border-slate-300 rounded px-2 py-1"
                >
                  <option value="approve">Approve</option>
                  <option value="reject">Reject</option>
                  <option value="maybe">Maybe</option>
                  <option value="pending">Pending</option>
                </select>
              </div>

              <div>
                <label className="block text-xs text-slate-600 mb-1">Status filter</label>
                <select
                  value={filterStatus}
                  onChange={e => {
                    setFilterStatus(e.target.value as SegmentStatus | '')
                    setPreviewCount(null)
                  }}
                  className="w-full text-sm border border-slate-300 rounded px-2 py-1"
                >
                  <option value="">Any</option>
                  {STATUS_VALUES.map(s => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-xs text-slate-600 mb-1">Min confidence</label>
                <input
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={minConfidence}
                  onChange={e => {
                    setMinConfidence(e.target.value)
                    setPreviewCount(null)
                  }}
                  placeholder="e.g. 0.85"
                  className="w-full text-sm border border-slate-300 rounded px-2 py-1"
                />
              </div>

              <div>
                <label className="block text-xs text-slate-600 mb-1">Max duration (s)</label>
                <input
                  type="number"
                  min={0}
                  step={0.5}
                  value={maxDuration}
                  onChange={e => {
                    setMaxDuration(e.target.value)
                    setPreviewCount(null)
                  }}
                  placeholder="e.g. 2.0"
                  className="w-full text-sm border border-slate-300 rounded px-2 py-1"
                />
              </div>

              {sources.length > 1 && (
                <div>
                  <label className="block text-xs text-slate-600 mb-1">Source</label>
                  <select
                    value={sourceId}
                    onChange={e => {
                      setSourceId(e.target.value)
                      setPreviewCount(null)
                    }}
                    className="w-full text-sm border border-slate-300 rounded px-2 py-1"
                  >
                    <option value="">All sources</option>
                    {sources.map(s => (
                      <option key={s.source_id} value={s.source_id}>{s.filename}</option>
                    ))}
                  </select>
                </div>
              )}
            </div>

            <div className="flex items-center gap-2 mt-3">
              <button
                onClick={handlePreview}
                disabled={previewing}
                className="text-sm px-3 py-1.5 rounded border border-slate-300 bg-white hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {previewing ? 'Loading…' : previewCount !== null ? `Preview (${previewCount})` : 'Preview'}
              </button>

              <button
                onClick={handleApply}
                disabled={applying || previewCount === null || previewCount === 0}
                className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {applying ? 'Applying…' : 'Apply'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
