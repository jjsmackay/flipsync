import { useState, useEffect } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useFilterState } from '../hooks/useFilterState'
import { useProjectPolling } from '../hooks/useProjectPolling'
import { getSegments, patchSegment } from '../api/client'
import type { Segment, SegmentStatus, PaginatedSegments } from '../types/api'
import { FilterBar } from '../components/review/FilterBar'
import { SegmentCard } from '../components/review/SegmentCard'
import { SegmentDetail } from '../components/review/SegmentDetail'
import { BulkOperations } from '../components/review/BulkOperations'
import { Timeline } from '../components/review/Timeline'
import { KeyboardHelp } from '../components/review/KeyboardHelp'
import { ExportButton } from '../components/export/ExportButton'
import { formatDuration } from '../utils/format'


export function ReviewQueuePage() {
  const { projectId } = useParams<{ projectId: string }>()
  const { filter, setFilter, toApiParams } = useFilterState()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [shortcutsEnabled, setShortcutsEnabled] = useState(true)
  const [showHelp, setShowHelp] = useState(false)
  const [showSpectrogram, setShowSpectrogram] = useState(false)
  const [segments, setSegments] = useState<Segment[]>([])
  const [pagination, setPagination] = useState({ page: 1, pages: 1, total: 0, per_page: 50 })
  const [segmentsLoading, setSegmentsLoading] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)

  // Project data (for ExportButton, sources list)
  const { project, refetch: refetchProject } = useProjectPolling(projectId!)


  // Fetch segments when filter or refreshKey changes
  useEffect(() => {
    if (!projectId) return
    setSegmentsLoading(true)
    getSegments(projectId, toApiParams())
      .then((result: PaginatedSegments) => {
        setSegments(result.segments)
        setPagination(result.pagination)
        setSelectedId(prev => {
          if (prev && result.segments.find(s => s.id === prev)) return prev
          return result.segments[0]?.id ?? null
        })
      })
      .finally(() => setSegmentsLoading(false))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, filter.status, filter.source_id, filter.min_confidence, filter.min_duration, filter.sort, filter.order, filter.page, refreshKey])

  const selectedSegment = segments.find(s => s.id === selectedId) ?? null
  const selectedIndex = segments.findIndex(s => s.id === selectedId)

  function selectNext() {
    if (selectedIndex < segments.length - 1) {
      setSelectedId(segments[selectedIndex + 1].id)
    } else if (filter.page < pagination.pages) {
      setFilter({ page: filter.page + 1 })
    }
  }

  function selectPrev() {
    if (selectedIndex > 0) {
      setSelectedId(segments[selectedIndex - 1].id)
    } else if (filter.page > 1) {
      setFilter({ page: filter.page - 1 })
    }
  }

  // Keyboard shortcuts (active when shortcutsEnabled)
  useEffect(() => {
    if (!shortcutsEnabled) return
    function onKey(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement).tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      switch (e.key) {
        case 'j':
        case 'J':
          e.preventDefault()
          selectNext()
          break
        case 'k':
        case 'K':
          e.preventDefault()
          selectPrev()
          break
        case 'a':
        case 'A':
          e.preventDefault()
          if (selectedSegment) void applyAction(selectedSegment, 'approved')
          break
        case 'm':
        case 'M':
          e.preventDefault()
          if (selectedSegment) void applyAction(selectedSegment, 'maybe')
          break
        case 'x':
        case 'X':
          e.preventDefault()
          if (selectedSegment) void applyAction(selectedSegment, 'rejected')
          break
        case '?':
          setShowHelp(h => !h)
          break
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shortcutsEnabled, selectedSegment, selectedIndex, segments, filter.page, pagination.pages])

  async function applyAction(segment: Segment, status: SegmentStatus) {
    try {
      await patchSegment(projectId!, segment.id, { status })
      setSegments(prev => prev.map(s => s.id === segment.id ? { ...s, status } : s))
      selectNext()
    } catch { /* silent — SegmentDetail shows errors */ }
  }

  function handleStatusChange(id: string, status: SegmentStatus) {
    setSegments(prev => prev.map(s => s.id === id ? { ...s, status } : s))
    selectNext()
  }

  function handleTranscriptChange(id: string, transcript: string) {
    setSegments(prev => prev.map(s => s.id === id ? { ...s, transcript_edited: transcript } : s))
  }

  const sources = project?.stats.source_coverage ?? []
  const timelineSpan = segments.reduce((max, s) => Math.max(max, s.end_secs), 0)

  return (
    <div className="h-screen flex flex-col bg-gray-50 overflow-hidden">
      {/* Top bar */}
      <div className="flex items-center gap-4 px-4 py-3 bg-white border-b border-gray-200 shrink-0">
        <Link to={`/projects/${projectId}`} className="text-gray-400 hover:text-gray-600 text-sm">
          ← {project?.name ?? 'Dashboard'}
        </Link>
        <span className="text-gray-300">|</span>
        {project && (
          <span className="text-sm text-gray-600">
            {formatDuration(project.stats.approved_duration_secs)} / {formatDuration(project.config.target_duration_secs)} approved
          </span>
        )}
        <span className="text-gray-300">|</span>
        <span className="text-sm text-gray-500">{pagination.total} segments</span>
        <div className="ml-auto flex items-center gap-2">
          <button onClick={() => setShowHelp(h => !h)} className="text-xs px-2 py-1 border border-gray-200 rounded hover:bg-gray-50">? Shortcuts</button>
          {project && <ExportButton project={project} />}
        </div>
      </div>

      {/* Filter bar */}
      <div className="px-4 py-2 shrink-0">
        <FilterBar filter={filter} sources={sources} onChange={setFilter} />
      </div>

      {/* Timeline */}
      {segments.length > 0 && (
        <div className="px-4 pb-2 shrink-0">
          <Timeline segments={segments} totalDuration={timelineSpan} selectedSegmentId={selectedId} onSegmentSelect={id => setSelectedId(id)} />
        </div>
      )}

      {/* Two-panel layout */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: list */}
        <div className="w-80 flex-none flex flex-col border-r border-gray-200 bg-white overflow-hidden">
          <div className="p-2 border-b border-gray-100 shrink-0">
            <BulkOperations
              projectId={projectId!}
              onApplied={() => { setRefreshKey(k => k + 1); void refetchProject() }}
              sources={sources.map(s => ({ source_id: s.source_id, filename: s.filename }))}
            />
          </div>
          <div className="flex-1 overflow-y-auto">
            {segmentsLoading && segments.length === 0 && (
              <div className="text-center py-8 text-gray-400 text-sm">Loading…</div>
            )}
            {!segmentsLoading && segments.length === 0 && (
              <div className="text-center py-8 text-gray-400 text-sm">No segments match your filters.</div>
            )}
            {segments.map(segment => (
              <SegmentCard
                key={segment.id}
                segment={segment}
                selected={segment.id === selectedId}
                onClick={() => setSelectedId(segment.id)}
              />
            ))}
          </div>
          {pagination.pages > 1 && (
            <div className="px-4 py-2 border-t border-gray-100 shrink-0 flex items-center justify-between text-sm">
              <button
                onClick={() => setFilter({ page: filter.page - 1 })}
                disabled={filter.page <= 1}
                className="text-gray-400 hover:text-gray-700 disabled:opacity-30"
              >
                ← Prev
              </button>
              <span className="text-gray-500 text-xs">{filter.page} / {pagination.pages}</span>
              <button
                onClick={() => setFilter({ page: filter.page + 1 })}
                disabled={filter.page >= pagination.pages}
                className="text-gray-400 hover:text-gray-700 disabled:opacity-30"
              >
                Next →
              </button>
            </div>
          )}
        </div>

        {/* Right: detail */}
        <div className="flex-1 overflow-hidden">
          {selectedSegment ? (
            <SegmentDetail
              projectId={projectId!}
              segment={selectedSegment}
              onStatusChange={handleStatusChange}
              onTranscriptChange={handleTranscriptChange}
              onFocusChange={setShortcutsEnabled}
              showSpectrogram={showSpectrogram}
              onSpectrogramToggle={() => setShowSpectrogram(s => !s)}
            />
          ) : (
            <div className="flex items-center justify-center h-full text-gray-400">
              {segments.length === 0 ? 'No segments to review' : 'Select a segment'}
            </div>
          )}
        </div>
      </div>

      {showHelp && <KeyboardHelp onClose={() => setShowHelp(false)} />}
    </div>
  )
}
