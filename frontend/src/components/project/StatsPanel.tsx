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
    <div className={`rounded-lg px-3 py-2 ${colorClass}`}>
      <p className="text-lg font-bold leading-tight">{value}</p>
      <p className="text-xs mt-0.5 opacity-75">{label}</p>
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
      <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
        <StatBox
          label="Approved"
          value={stats.approved_count}
          colorClass="bg-green-50 text-green-800 dark:bg-green-900/30 dark:text-green-300"
        />
        <StatBox
          label="Auto-approved"
          value={stats.auto_approved_count}
          colorClass="bg-teal-50 text-teal-800 dark:bg-teal-900/30 dark:text-teal-300"
        />
        <StatBox
          label="Pending"
          value={stats.pending_count}
          colorClass="bg-gray-50 text-gray-800 dark:bg-gray-800 dark:text-gray-200"
        />
        <StatBox
          label="Maybe"
          value={stats.maybe_count}
          colorClass="bg-yellow-50 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300"
        />
        <StatBox
          label="Rejected"
          value={stats.rejected_count}
          colorClass="bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-300"
        />
        <StatBox
          label="Below threshold"
          value={stats.below_threshold_count}
          colorClass="bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400"
        />
      </div>

      <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
        <p className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
          Approved duration
          <span className="ml-2 text-xs font-normal text-gray-400 dark:text-gray-500">(includes auto-approved)</span>
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
