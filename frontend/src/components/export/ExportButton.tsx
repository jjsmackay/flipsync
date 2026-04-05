import { useState } from 'react'
import type { ProjectDetail } from '../../types/api'
import { triggerExport, getExportDownloadUrl } from '../../api/client'

type ExportState = 'idle' | 'confirm' | 'exporting' | 'ready'

interface ExportButtonProps {
  project: ProjectDetail
}

function formatDuration(secs: number): string {
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

export function ExportButton({ project }: ExportButtonProps) {
  const [state, setState] = useState<ExportState>('idle')
  const [exportError, setExportError] = useState<string | null>(null)

  const approvedCount = project.stats.approved_count
  const approvedDuration = project.stats.approved_duration_secs
  const hasLowCoverage = project.stats.source_coverage.some(s => s.low_coverage_warning)

  if (approvedCount === 0) {
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
        Export ({approvedCount} · {formatDuration(approvedDuration)})
      </button>
    )
  }

  if (state === 'confirm') {
    return (
      <div className="flex items-center gap-3 px-3 py-2 bg-blue-50 border border-blue-200 rounded text-xs">
        <div className="text-gray-700">
          <span className="font-medium">{approvedCount} segments</span>
          {' · '}
          <span>{formatDuration(approvedDuration)}</span>
          {hasLowCoverage && (
            <span className="ml-2 text-amber-600">⚠ Some sources have low coverage</span>
          )}
        </div>
        <button
          onClick={() => setState('idle')}
          className="px-2 py-1 text-gray-500 hover:text-gray-700 border border-gray-300 rounded bg-white"
        >
          Cancel
        </button>
        <button
          onClick={async () => {
            setState('exporting')
            setExportError(null)
            try {
              await triggerExport(project.id)
              setState('ready')
            } catch (err) {
              setExportError(err instanceof Error ? err.message : String(err))
              setState('confirm')
            }
          }}
          className="px-2 py-1 text-white bg-blue-600 hover:bg-blue-700 border border-blue-600 rounded"
        >
          Export
        </button>
        {exportError && (
          <span className="text-red-600">{exportError}</span>
        )}
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
