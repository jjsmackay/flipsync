interface ConfidenceBadgeProps {
  value: number
  label?: string
  title?: string
}

function confidenceStyle(v: number): string {
  if (v >= 0.9) return 'text-green-700 bg-green-50'
  if (v >= 0.75) return 'text-yellow-700 bg-yellow-50'
  return 'text-red-700 bg-red-50'
}

export function ConfidenceBadge({ value, label, title }: ConfidenceBadgeProps) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-mono font-medium ${confidenceStyle(value)} ${title ? 'cursor-help' : ''}`}
    >
      {label && <span className="font-sans text-xs opacity-70">{label}</span>}
      {(value * 100).toFixed(0)}%
    </span>
  )
}
