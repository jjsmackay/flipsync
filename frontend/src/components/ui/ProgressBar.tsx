interface ProgressBarProps {
  value: number
  label?: string
  className?: string
  color?: 'green' | 'blue' | 'yellow'
}

const COLORS = {
  green: 'bg-green-500',
  blue: 'bg-blue-500',
  yellow: 'bg-yellow-500',
}

export function ProgressBar({ value, label, className = '', color = 'green' }: ProgressBarProps) {
  const pct = Math.min(1, Math.max(0, value)) * 100

  return (
    <div className={`w-full ${className}`}>
      {label && (
        <div className="flex justify-between text-xs text-gray-500 mb-1">
          <span>{label}</span>
          <span>{pct.toFixed(0)}%</span>
        </div>
      )}
      <div className="w-full bg-gray-200 rounded-full h-2">
        <div
          className={`h-2 rounded-full transition-all ${COLORS[color]}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}
