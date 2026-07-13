/** Format seconds as human-readable duration (e.g. "2h 15m", "3m 12s", "8s"). */
export function formatDuration(secs: number): string {
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = Math.floor(secs % 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

/** Format seconds as coarse duration without seconds (e.g. "2h 15m", "3m"). */
export function formatDurationCoarse(secs: number): string {
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

/** Format seconds as a zero-padded HH:MM:SS timestamp (e.g. "00:01:23"). */
export function formatTimestamp(secs: number): string {
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = Math.floor(secs % 60)
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
}

/** Format seconds as a bare M:SS clock, no hours (e.g. "1:23"). */
export function formatClock(secs: number): string {
  const m = Math.floor(secs / 60)
  const s = Math.floor(secs % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

/** Format seconds to one decimal place with a trailing "s" (e.g. "3.4s"). */
export function formatSecondsPrecise(secs: number): string {
  return `${secs.toFixed(1)}s`
}
