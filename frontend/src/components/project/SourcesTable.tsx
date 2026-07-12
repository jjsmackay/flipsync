import { useState } from 'react'
import type { SourceCoverage } from '../../types/api'
import { StatusBadge } from '../ui/StatusBadge'

interface SourcesTableProps {
  sources: SourceCoverage[]
  onReprocess?: (sourceId: string, steps: string[]) => void
}

const REPROCESS_ACTIONS: { label: string; steps: string[] }[] = [
  { label: 'Re-run from vocal separation', steps: ['separation', 'diarisation'] },
  { label: 'Re-run speaker matching', steps: ['diarisation'] },
]

export function SourcesTable({ sources, onReprocess }: SourcesTableProps) {
  const [menuFor, setMenuFor] = useState<string | null>(null)

  if (sources.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400 py-4">No videos uploaded yet.</p>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-200 dark:border-gray-700 text-left text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide">
            <th className="pb-2 pr-4 font-medium">File</th>
            <th className="pb-2 pr-4 font-medium">Status</th>
            <th className="pb-2 pr-4 font-medium">Speaker coverage</th>
            {onReprocess && (
              <th className="pb-2 font-medium">
                <span className="sr-only">Actions</span>
              </th>
            )}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
          {sources.map((src) => {
            const hasCoverage = src.coverage_ratio != null && src.status === 'complete'
            const coveragePct = Math.round((src.coverage_ratio ?? 0) * 100)
            const coverageColor = src.low_coverage_warning
              ? 'text-amber-600'
              : coveragePct >= 50
              ? 'text-green-600'
              : 'text-red-600'

            return (
              <tr key={src.source_id} className="py-2">
                <td className="py-2 pr-4">
                  <span className="font-medium text-gray-800 dark:text-gray-200">{src.filename}</span>
                  {src.error && (
                    <p className="text-xs text-red-500 dark:text-red-400 mt-0.5">{src.error}</p>
                  )}
                </td>
                <td className="py-2 pr-4">
                  <StatusBadge status={src.status} />
                </td>
                <td className="py-2 pr-4">
                  {hasCoverage ? (
                    <>
                      <span className={`font-medium ${coverageColor}`}>{coveragePct}%</span>
                      {src.low_coverage_warning && (
                        <span className="ml-1 text-xs text-amber-500 dark:text-amber-400">low</span>
                      )}
                    </>
                  ) : (
                    <span className="text-gray-400 dark:text-gray-500">—</span>
                  )}
                </td>
                {onReprocess && (
                  <td className="py-2 text-right">
                    <div className="relative inline-block">
                      <button
                        onClick={() => setMenuFor(menuFor === src.source_id ? null : src.source_id)}
                        aria-label={`Actions for ${src.filename}`}
                        className="px-2 py-1 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"
                      >
                        ⋯
                      </button>
                      {menuFor === src.source_id && (
                        <>
                          <div className="fixed inset-0 z-10" onClick={() => setMenuFor(null)} />
                          <div className="absolute right-0 z-20 mt-1 w-60 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-lg py-1 text-left">
                            {REPROCESS_ACTIONS.map((action) => (
                              <button
                                key={action.label}
                                onClick={() => {
                                  setMenuFor(null)
                                  onReprocess(src.source_id, action.steps)
                                }}
                                className="block w-full px-3 py-2 text-sm text-left text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/50"
                              >
                                {action.label}
                              </button>
                            ))}
                          </div>
                        </>
                      )}
                    </div>
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
