import type { ProjectDetailStats, ProjectConfig } from '../../types/api'
import { formatDurationCoarse as formatDuration } from '../../utils/format'
import { ProgressBar } from '../ui/ProgressBar'

interface StatsPanelProps {
  stats: ProjectDetailStats
  config: ProjectConfig
}


interface StatBoxProps {
  label: string
  value: number
  colorClass: string
}

function StatBox({ label, value, colorClass }: StatBoxProps) {
  return (
    <div className={`rounded-lg p-4 ${colorClass}`}>
      <p className="text-2xl font-bold">{value}</p>
      <p className="text-sm mt-1 opacity-75">{label}</p>
    </div>
  )
}

export function StatsPanel({ stats, config }: StatsPanelProps) {
  const progressValue = config.target_duration_secs > 0
    ? (stats.approved_duration_secs / config.target_duration_secs) * 100
    : 0

  const approvedLabel = `${formatDuration(stats.approved_duration_secs)} / ${formatDuration(config.target_duration_secs)}`

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        <StatBox
          label="Approved"
          value={stats.approved_count}
          colorClass="bg-green-50 text-green-800"
        />
        <StatBox
          label="Pending"
          value={stats.pending_count}
          colorClass="bg-gray-50 text-gray-800"
        />
        <StatBox
          label="Maybe"
          value={stats.maybe_count}
          colorClass="bg-yellow-50 text-yellow-800"
        />
        <StatBox
          label="Rejected"
          value={stats.rejected_count}
          colorClass="bg-red-50 text-red-800"
        />
        <StatBox
          label="Below threshold"
          value={stats.below_threshold_count}
          colorClass="bg-gray-100 text-gray-600"
        />
      </div>

      <div className="bg-white border border-gray-200 rounded-lg p-4">
        <p className="text-sm font-medium text-gray-700 mb-2">
          Approved duration
        </p>
        <ProgressBar
          value={progressValue}
          label={approvedLabel}
          color="green"
        />
      </div>
    </div>
  )
}
