import { useState, useEffect } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useFilterState } from '../hooks/useFilterState'
import { useProjectPolling } from '../hooks/useProjectPolling'
import { getSegments, patchSegment, ApiError } from '../api/client'
import type { Segment, SegmentStatus, PaginatedSegments } from '../types/api'
import { ALL_SEGMENT_STATUSES_CSV } from '../constants'
import { FilterBar } from '../components/review/FilterBar'
import { SegmentCard } from '../components/review/SegmentCard'
import { SegmentDetail } from '../components/review/SegmentDetail'
import { BulkOperations } from '../components/review/BulkOperations'
import { Timeline } from '../components/review/Timeline'
import { KeyboardHelp } from '../components/review/KeyboardHelp'
import { ExportButton } from '../components/export/ExportButton'
import { ThemeToggle } from '../components/ui/ThemeToggle'
import { formatDuration } from '../utils/format'
import { isWorkQueueFilter } from '../utils/reviewFilters'


export function ReviewQueuePage() {
  const { projectId } = useParams<{ projectId: string }>()
  const { filter, setFilter, toApiParams } = useFilterState()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [shortcutsEnabled, setShortcutsEnabled] = useState(true)
  const [showHelp, setShowHelp] = useState(false)
  const [showSpectrogram, setShowSpectrogram] = useState(false)
  const [autoPlay, setAutoPlay] = useState(false)
  const [segments, setSegments] = useState<Segment[]>([])
  const [pagination, setPagination] = useState({ page: 1, pages: 1, total: 0, per_page: 50 })
  const [segmentsLoading, setSegmentsLoading] = useState(false)
  const [fetchError, setFetchError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [timelineSegments, setTimelineSegments] = useState<Segment[]>([])
  const [refreshKey, setRefreshKey] = useState(0)

  // Project data (for ExportButton, sources list)
  const { project, refetch: refetchProject } = useProjectPolling(projectId!)

  // Fetch segments when filter or refreshKey changes (abort-guarded so a slow response
  // can't overwrite a newer one).
  useEffect(() => {
    if (!projectId) return
    const controller = new AbortController()
    let active = true
    setSegmentsLoading(true)
    setFetchError(null)
    getSegments(projectId, toApiParams(), controller.signal)
      .then((result: PaginatedSegments) => {
        if (!active) return
        setSegments(result.segments)
        setPagination(result.pagination)
        setSelectedId(prev => {
          if (prev && result.segments.find(s => s.id === prev)) return prev
          return result.segments[0]?.id ?? null
        })
      })
      .catch((err) => {
        if (!active || controller.signal.aborted) return
        setFetchError(err instanceof Error ? err.message : 'Failed to load segments')
      })
      .finally(() => {
        if (active) setSegmentsLoading(false)
      })
    return () => {
      active = false
      controller.abort()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, filter.status, filter.source_id, filter.min_confidence, filter.min_duration, filter.sort, filter.order, filter.page, refreshKey])

  // Fetch ALL segments for the current source (every status), for the timeline — so the
  // axis and bars reflect the whole source rather than just the current review page.
  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    async function loadAll() {
      const all: Segment[] = []
      let page = 1
      let pages = 1
      do {
        const res = await getSegments(projectId!, {
          status: ALL_SEGMENT_STATUSES_CSV,
          source_id: filter.source_id || undefined,
          sort: 'start_secs',
          order: 'asc',
          page,
          per_page: 200,
        })
        if (cancelled) return
        all.push(...res.segments)
        pages = res.pagination.pages
        page++
      } while (page <= pages)
      if (!cancelled) setTimelineSegments(all)
    }
    loadAll().catch(() => {
      if (!cancelled) setTimelineSegments([])
    })
    return () => {
      cancelled = true
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, filter.source_id, refreshKey])

  // Auto-clear transient action errors.
  useEffect(() => {
    if (!actionError) return
    const t = setTimeout(() => setActionError(null), 4000)
    return () => clearTimeout(t)
  }, [actionError])

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
    // Advance optimistically for a snappy keyboard flow, but surface failures so a
    // rejected transition (e.g. 409) doesn't look like a dead key.
    try {
      await patchSegment(projectId!, segment.id, { status })
      setSegments(prev => prev.map(s => s.id === segment.id ? { ...s, status } : s))
      selectNext()
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : 'Action failed'
      setActionError(msg)
    }
  }

  function handleStatusChange(id: string, status: SegmentStatus) {
    setSegments(prev => prev.map(s => s.id === id ? { ...s, status } : s))
    selectNext()
  }

  function handleTranscriptChange(id: string, transcript: string | null) {
    setSegments(prev => prev.map(s => s.id === id ? { ...s, transcript_edited: transcript } : s))
  }

  const sources = project?.stats.source_coverage ?? []
  // Axis spans the source: use the furthest segment end across all fetched segments.
  const timelineSpan = timelineSegments.reduce((max, s) => Math.max(max, s.end_secs), 0)

  const activeTranscription = project?.active_jobs.find(
    j => j.type === 'transcription_bulk' || j.type === 'transcription',
  )
  const lowCoverage = sources.some(s => s.low_coverage_warning)

  // "All reviewed" completion state: the work queue (pending/maybe) is empty but the
  // project does have segments. Set comparison, not substring — 'All' plus restrictive
  // secondary filters must show the no-match state, not "You've reviewed all segments".
  const isWorkQueue = isWorkQueueFilter(filter.status)
  const showCompletion =
    !segmentsLoading &&
    !fetchError &&
    segments.length === 0 &&
    isWorkQueue &&
    (project?.stats.total_segments ?? 0) > 0

  return (
    <div className="h-screen flex flex-col bg-gray-50 dark:bg-gray-900 overflow-hidden">
      {/* Top bar */}
      <div className="flex items-center gap-4 px-4 py-3 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 shrink-0">
        <Link to={`/projects/${projectId}`} className="text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 text-sm">
          ← {project?.name ?? 'Dashboard'}
        </Link>
        <span className="text-gray-300 dark:text-gray-600">|</span>
        {project && (
          <span className="text-sm text-gray-600 dark:text-gray-400">
            {formatDuration(project.stats.approved_duration_secs)} / {formatDuration(project.config.target_duration_secs)} approved
          </span>
        )}
        <span className="text-gray-300 dark:text-gray-600">|</span>
        <span className="text-sm text-gray-500 dark:text-gray-400">{pagination.total} segments</span>
        <div className="ml-auto flex items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-gray-600 dark:text-gray-400 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={autoPlay}
              onChange={e => setAutoPlay(e.target.checked)}
              className="accent-indigo-600"
            />
            Auto-play
          </label>
          <button onClick={() => setShowHelp(h => !h)} className="text-xs px-2 py-1 border border-gray-200 dark:border-gray-700 rounded hover:bg-gray-50 dark:hover:bg-gray-700/50 text-gray-700 dark:text-gray-300">? Shortcuts</button>
          <ThemeToggle />
          {project && <ExportButton project={project} onStarted={() => void refetchProject()} />}
        </div>
      </div>

      {/* Banners */}
      {activeTranscription && (
        <div className="px-4 py-2 bg-blue-50 dark:bg-blue-900/20 border-b border-blue-100 dark:border-blue-800 text-xs text-blue-700 dark:text-blue-400 shrink-0">
          Transcription in progress
          {activeTranscription.progress !== null ? ` — ${Math.round(activeTranscription.progress)}% complete` : '…'}
        </div>
      )}
      {lowCoverage && (
        <div className="px-4 py-2 bg-amber-50 dark:bg-amber-900/20 border-b border-amber-100 dark:border-amber-800 text-xs text-amber-700 dark:text-amber-400 shrink-0">
          Some source files have low target speaker coverage. Check the dashboard for details. Your dataset may be thinner than expected.
        </div>
      )}

      {/* Filter bar */}
      <div className="px-4 py-2 shrink-0">
        <FilterBar filter={filter} sources={sources} onChange={setFilter} />
      </div>

      {/* Timeline */}
      {timelineSegments.length > 0 && timelineSpan > 0 && (
        <div className="px-4 pb-2 shrink-0">
          <Timeline segments={timelineSegments} totalDuration={timelineSpan} selectedSegmentId={selectedId} onSegmentSelect={id => setSelectedId(id)} />
        </div>
      )}

      {/* Two-panel layout */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: list */}
        <div className="w-80 flex-none flex flex-col border-r border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 overflow-hidden">
          <div className="p-2 border-b border-gray-100 dark:border-gray-800 shrink-0">
            <BulkOperations
              projectId={projectId!}
              onApplied={() => { setRefreshKey(k => k + 1); void refetchProject() }}
              sources={sources.map(s => ({ source_id: s.source_id, filename: s.filename }))}
            />
          </div>
          <div className="flex-1 overflow-y-auto">
            {segmentsLoading && segments.length === 0 && (
              <div className="text-center py-8 text-gray-400 dark:text-gray-500 text-sm">Loading…</div>
            )}
            {fetchError && (
              <div className="m-3 text-sm text-red-700 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded px-3 py-2">
                {fetchError}
              </div>
            )}
            {!segmentsLoading && !fetchError && segments.length === 0 && (
              showCompletion && project ? (
                <div className="px-4 py-8 text-sm text-gray-500 dark:text-gray-400 text-center space-y-2">
                  <p className="font-medium text-gray-700 dark:text-gray-300">You've reviewed all segments in this filter.</p>
                  <p>
                    {project.stats.approved_count} approved, {project.stats.rejected_count} rejected,{' '}
                    {project.stats.maybe_count} in Maybe.
                  </p>
                  {project.stats.maybe_count > 0 && (
                    <button
                      onClick={() => setFilter({ status: 'maybe' })}
                      className="text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300 underline"
                    >
                      View the Maybe pile
                    </button>
                  )}
                </div>
              ) : (
                <div className="text-center py-8 text-gray-400 dark:text-gray-500 text-sm px-4">
                  No segments match the current filters. Try widening the confidence threshold or changing the status filter.
                </div>
              )
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
            <div className="px-4 py-2 border-t border-gray-100 dark:border-gray-800 shrink-0 flex items-center justify-between text-sm">
              <button
                onClick={() => setFilter({ page: filter.page - 1 })}
                disabled={filter.page <= 1}
                className="text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 disabled:opacity-30"
              >
                ← Prev
              </button>
              <span className="text-gray-500 dark:text-gray-400 text-xs">{filter.page} / {pagination.pages}</span>
              <button
                onClick={() => setFilter({ page: filter.page + 1 })}
                disabled={filter.page >= pagination.pages}
                className="text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 disabled:opacity-30"
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
              autoPlay={autoPlay}
            />
          ) : (
            <div className="flex items-center justify-center h-full text-gray-400 dark:text-gray-500">
              {segments.length === 0 ? 'No segments to review' : 'Select a segment'}
            </div>
          )}
        </div>
      </div>

      {/* Transient action error toast */}
      {actionError && (
        <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-50 bg-red-600 text-white text-sm px-4 py-2 rounded shadow-lg">
          {actionError}
        </div>
      )}

      {showHelp && <KeyboardHelp onClose={() => setShowHelp(false)} />}
    </div>
  )
}
