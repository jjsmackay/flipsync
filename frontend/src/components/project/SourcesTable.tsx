import type { SourceCoverage } from '../../types/api'
import { StatusBadge } from '../ui/StatusBadge'

interface SourcesTableProps {
  sources: SourceCoverage[]
  onReprocess?: (sourceId: string, steps: string[]) => void
}

export function SourcesTable({ sources, onReprocess }: SourcesTableProps) {
  if (sources.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400 py-4">No sources uploaded yet.</p>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-200 dark:border-gray-700 text-left text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide">
            <th className="pb-2 pr-4 font-medium">File</th>
            <th className="pb-2 pr-4 font-medium">Status</th>
            <th className="pb-2 pr-4 font-medium">Coverage</th>
            {onReprocess && <th className="pb-2 font-medium">Actions</th>}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
          {sources.map((src) => {
            const coveragePct = Math.round(src.coverage_ratio * 100)
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
                  <span className={`font-medium ${coverageColor}`}>
                    {coveragePct}%
                  </span>
                  {src.low_coverage_warning && (
                    <span className="ml-1 text-xs text-amber-500 dark:text-amber-400">low</span>
                  )}
                </td>
                {onReprocess && (
                  <td className="py-2">
                    <div className="flex gap-2">
                      <button
                        onClick={() => onReprocess(src.source_id, ['separation', 'diarisation'])}
                        className="px-2 py-1 text-xs bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-300 rounded transition-colors"
                      >
                        Step 1+2
                      </button>
                      <button
                        onClick={() => onReprocess(src.source_id, ['diarisation'])}
                        className="px-2 py-1 text-xs bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-300 rounded transition-colors"
                      >
                        Step 2
                      </button>
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
