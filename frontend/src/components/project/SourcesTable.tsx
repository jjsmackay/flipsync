import type { SourceCoverage } from '../../types/api'
import { StatusBadge } from '../ui/StatusBadge'

interface SourcesTableProps {
  sources: SourceCoverage[]
  onReprocess?: (sourceId: string, steps: string[]) => void
}

export function SourcesTable({ sources, onReprocess }: SourcesTableProps) {
  if (sources.length === 0) {
    return (
      <p className="text-sm text-gray-500 py-4">No sources uploaded yet.</p>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-200 text-left text-xs text-gray-500 uppercase tracking-wide">
            <th className="pb-2 pr-4 font-medium">File</th>
            <th className="pb-2 pr-4 font-medium">Status</th>
            <th className="pb-2 pr-4 font-medium">Coverage</th>
            {onReprocess && <th className="pb-2 font-medium">Actions</th>}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
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
                  <span className="font-medium text-gray-800">{src.filename}</span>
                  {src.error && (
                    <p className="text-xs text-red-500 mt-0.5">{src.error}</p>
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
                    <span className="ml-1 text-xs text-amber-500">low</span>
                  )}
                </td>
                {onReprocess && (
                  <td className="py-2">
                    <div className="flex gap-2">
                      <button
                        onClick={() => onReprocess(src.source_id, ['vocal_separation', 'diarisation'])}
                        className="px-2 py-1 text-xs bg-gray-100 hover:bg-gray-200 text-gray-700 rounded transition-colors"
                      >
                        Step 1+2
                      </button>
                      <button
                        onClick={() => onReprocess(src.source_id, ['diarisation'])}
                        className="px-2 py-1 text-xs bg-gray-100 hover:bg-gray-200 text-gray-700 rounded transition-colors"
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
