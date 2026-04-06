interface AudioControlsProps {
  isPlaying: boolean
  currentTime: number
  duration: number
  playbackRate: number
  onToggle: () => void
  onRestart: () => void
  onSpeedChange: (rate: number) => void
}

const SPEED_OPTIONS = [0.75, 1, 1.25, 1.5]

function formatTime(secs: number): string {
  const m = Math.floor(secs / 60)
  const s = Math.floor(secs % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

export function AudioControls({
  isPlaying,
  currentTime,
  duration,
  playbackRate,
  onToggle,
  onRestart,
  onSpeedChange,
}: AudioControlsProps) {
  return (
    <div className="flex items-center gap-3">
      {/* Restart */}
      <button
        type="button"
        onClick={onRestart}
        title="Restart (R)"
        className="text-gray-500 hover:text-gray-800 text-lg leading-none focus:outline-none focus:ring-2 focus:ring-indigo-400 rounded"
      >
        ↩
      </button>

      {/* Play/Pause */}
      <button
        type="button"
        onClick={onToggle}
        title="Play/Pause (Space)"
        className="w-9 h-9 rounded-full bg-indigo-600 hover:bg-indigo-700 text-white flex items-center justify-center focus:outline-none focus:ring-2 focus:ring-indigo-400 focus:ring-offset-1 text-base"
      >
        {isPlaying ? '⏸' : '▶'}
      </button>

      {/* Time display */}
      <span className="font-mono text-xs text-gray-600 tabular-nums min-w-[72px]">
        {formatTime(currentTime)} / {formatTime(duration)}
      </span>

      {/* Speed buttons */}
      <div className="flex items-center gap-1 ml-auto">
        {SPEED_OPTIONS.map(rate => (
          <button
            key={rate}
            type="button"
            onClick={() => onSpeedChange(rate)}
            className={[
              'px-1.5 py-0.5 rounded text-xs font-mono focus:outline-none focus:ring-2 focus:ring-indigo-400',
              playbackRate === rate
                ? 'bg-indigo-600 text-white'
                : 'bg-gray-100 text-gray-600 hover:bg-gray-200',
            ].join(' ')}
          >
            {rate}×
          </button>
        ))}
      </div>
    </div>
  )
}
