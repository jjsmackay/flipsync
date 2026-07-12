import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import type { ProjectDetail, Segment } from '../../types/api'
import { triggerExport, getExportDownloadUrl, getSegments } from '../../api/client'
import { formatDurationCoarse as formatDuration } from '../../utils/format'

type ExportState = 'idle' | 'confirm' | 'exporting' | 'ready'

interface ExportButtonProps {
  project: ProjectDetail
  /** Called right after export is triggered so the parent can refetch immediately. */
  onStarted?: () => void
}

interface ExportCounts {
  clipping: number
  noTranscript: number
}

export function ExportButton({ project, onStarted }: ExportButtonProps) {
  const [state, setState] = useState<ExportState>(project.status === 'exported' ? 'ready' : 'idle')
  const [exportError, setExportError] = useState<string | null>(null)
  const [exportJobId, setExportJobId] = useState<string | null>(null)
  const [counts, setCounts] = useState<ExportCounts | null>(null)

  const approvedCount = project.stats.approved_count
  const autoApprovedCount = project.stats.auto_approved_count
  // Export includes both approved and auto_approved segments; approved_duration_secs
  // already covers both (drives the progress bar too).
  const exportCount = approvedCount + autoApprovedCount
  const approvedDuration = project.stats.approved_duration_secs
  const hasLowCoverage = project.stats.source_coverage.some(s => s.low_coverage_warning)

  // React to polled project changes: advance the export state machine (SC6) and keep
  // an idle/ready button in sync with the project status.
  useEffect(() => {
    if (state === 'exporting') {
      if (!exportJobId) return
      if (project.active_jobs.some(j => j.id === exportJobId)) return
      const failed = project.recent_failed_jobs.find(j => j.id === exportJobId)
      if (failed) {
        setExportError(failed.error ?? 'Export failed.')
        setState('confirm')
      } else if (project.status === 'exported') {
        setState('ready')
      }
      // otherwise the job has left active_jobs but status isn't 'exported' yet — wait.
      return
    }
    if (state === 'idle' && project.status === 'exported') setState('ready')
    if (state === 'ready' && project.status !== 'exported') setState('idle')
  }, [project, state, exportJobId])

  // When the confirmation panel is open, compute the real clipping / missing-transcript
  // counts among the approved segments that will be exported.
  useEffect(() => {
    if (state !== 'confirm') return
    let cancelled = false
    setCounts(null)
    ;(async () => {
      const approved: Segment[] = []
      let page = 1
      let pages = 1
      do {
        const res = await getSegments(project.id, {
          status: 'approved,auto_approved',
          sort: 'start_secs',
          order: 'asc',
          page,
          per_page: 200,
        })
        if (cancelled) return
        approved.push(...res.segments)
        pages = res.pagination.pages
        page++
      } while (page <= pages)
      if (cancelled) return
      setCounts({
        clipping: approved.filter(s => s.clipping_warning).length,
        noTranscript: approved.filter(s => !(s.transcript_edited ?? s.transcript)).length,
      })
    })().catch(() => {
      if (!cancelled) setCounts(null)
    })
    return () => {
      cancelled = true
    }
  }, [state, project.id])

  async function handleExport() {
    setExportError(null)
    setState('exporting')
    try {
      const res = await triggerExport(project.id)
      setExportJobId(res.enqueued_job.id)
      onStarted?.()
    } catch (err) {
      setExportError(err instanceof Error ? err.message : String(err))
      setState('confirm')
    }
  }

  if (exportCount === 0) {
    return (
      <button
        disabled
        className="text-xs px-3 py-1.5 bg-gray-100 text-gray-400 rounded cursor-not-allowed border border-gray-200"
      >
        Export (no approvals)
      </button>
    )
  }

  if (state === 'idle') {
    return (
      <button
        onClick={() => setState('confirm')}
        className="text-xs px-3 py-1.5 bg-blue-600 text-white rounded hover:bg-blue-700 border border-blue-600"
      >
        Export ({exportCount} · {formatDuration(approvedDuration)})
      </button>
    )
  }

  if (state === 'confirm') {
    return (
      <div className="flex flex-col gap-2 px-3 py-2 bg-blue-50 border border-blue-200 rounded text-xs">
        <div className="text-gray-700">
          <p className="font-medium">
            {exportCount} segments ({approvedCount} approved · {autoApprovedCount} auto-approved) · {formatDuration(approvedDuration)} of audio
          </p>
          {counts === null ? (
            <p className="text-gray-400 mt-1">Checking segments…</p>
          ) : (
            <>
              {counts.clipping > 0 && (
                <p className="text-amber-600 mt-1">
                  Segments with clipping warnings: {counts.clipping}{' '}
                  <Link
                    to={`/projects/${project.id}/review?status=clipping_warning`}
                    className="underline hover:text-amber-700"
                  >
                    review
                  </Link>
                </p>
              )}
              {counts.noTranscript > 0 && (
                <p className="text-amber-600 mt-0.5">Segments without transcripts: {counts.noTranscript}</p>
              )}
            </>
          )}
          <p className="text-gray-500 mt-1">
            This will clean and normalise all approved segments. The previous export (if any) will be replaced.
          </p>
          {hasLowCoverage && (
            <p className="text-amber-600 mt-1">Some sources have low target speaker coverage.</p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setState('idle')}
            className="px-2 py-1 text-gray-500 hover:text-gray-700 border border-gray-300 rounded bg-white"
          >
            Cancel
          </button>
          <button
            onClick={() => void handleExport()}
            className="px-2 py-1 text-white bg-blue-600 hover:bg-blue-700 border border-blue-600 rounded"
          >
            Export
          </button>
          {exportError && (
            <span className="text-red-600">{exportError}</span>
          )}
        </div>
      </div>
    )
  }

  if (state === 'exporting') {
    return (
      <button
        disabled
        className="text-xs px-3 py-1.5 bg-blue-400 text-white rounded cursor-not-allowed border border-blue-400"
      >
        Exporting…
      </button>
    )
  }

  // state === 'ready'
  return (
    <a
      href={getExportDownloadUrl(project.id)}
      download
      className="text-xs px-3 py-1.5 bg-green-600 text-white rounded hover:bg-green-700 border border-green-600 no-underline"
    >
      ↓ Download export
    </a>
  )
}
