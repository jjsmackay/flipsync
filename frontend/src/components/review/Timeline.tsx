import { useRef, useState, useEffect } from 'react'
import type { Segment, SegmentStatus } from '../../types/api'

interface TimelineProps {
  segments: Segment[]
  totalDuration: number
  selectedSegmentId: string | null
  onSegmentSelect: (id: string) => void
  visibleRange?: [number, number]
}

const STATUS_COLORS: Record<SegmentStatus, string> = {
  approved: '#22c55e',
  rejected: '#ef4444',
  maybe: '#f59e0b',
  pending: '#94a3b8',
  below_threshold: '#e2e8f0',
  clipping_warning: '#f97316',
  auto_rejected: '#fca5a5',
}

const SELECTED_COLOR = '#3b82f6'
const CANVAS_HEIGHT = 32

export function Timeline({
  segments,
  totalDuration,
  selectedSegmentId,
  onSegmentSelect,
  visibleRange,
}: TimelineProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [zoom, setZoom] = useState(1)
  const [offsetSecs, setOffsetSecs] = useState(0)

  // Effective visible window in seconds
  const windowDuration = totalDuration / zoom
  // Clamp offset so window stays within [0, totalDuration]
  const clampedOffset = Math.max(0, Math.min(offsetSecs, totalDuration - windowDuration))

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const width = canvas.width
    const height = canvas.height

    ctx.clearRect(0, 0, width, height)

    // Background
    ctx.fillStyle = '#f1f5f9'
    ctx.fillRect(0, 0, width, height)

    const viewStart = clampedOffset
    const viewEnd = clampedOffset + windowDuration

    for (const seg of segments) {
      // Skip segments entirely outside view
      if (seg.end_secs < viewStart || seg.start_secs > viewEnd) continue

      const visStart = Math.max(seg.start_secs, viewStart)
      const visEnd = Math.min(seg.end_secs, viewEnd)

      const x = ((visStart - viewStart) / windowDuration) * width
      const w = ((visEnd - visStart) / windowDuration) * width

      // Segments narrower than 2px still render as a single-pixel mark so they
      // aren't invisible at season scale (spec: timeline component).
      const drawW = w < 2 ? 1 : Math.ceil(w)

      ctx.fillStyle = seg.id === selectedSegmentId ? SELECTED_COLOR : STATUS_COLORS[seg.status]
      ctx.fillRect(Math.floor(x), 0, drawW, height)
    }

    // Respect visibleRange overlay (optional: dim out-of-range areas)
    if (visibleRange) {
      const [rangeStart, rangeEnd] = visibleRange
      const rxStart = ((Math.max(rangeStart, viewStart) - viewStart) / windowDuration) * width
      const rxEnd = ((Math.min(rangeEnd, viewEnd) - viewStart) / windowDuration) * width

      // Dim area before visibleRange
      if (rxStart > 0) {
        ctx.fillStyle = 'rgba(0,0,0,0.15)'
        ctx.fillRect(0, 0, rxStart, height)
      }
      // Dim area after visibleRange
      if (rxEnd < width) {
        ctx.fillStyle = 'rgba(0,0,0,0.15)'
        ctx.fillRect(rxEnd, 0, width - rxEnd, height)
      }
    }
  }, [segments, totalDuration, selectedSegmentId, zoom, clampedOffset, windowDuration, visibleRange])

  function handleClick(e: React.MouseEvent<HTMLCanvasElement>) {
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const xRatio = (e.clientX - rect.left) / rect.width
    const clickTime = clampedOffset + xRatio * windowDuration

    // Find the segment whose range contains clickTime; prefer selected, then first match
    let best: Segment | null = null
    for (const seg of segments) {
      if (seg.start_secs <= clickTime && seg.end_secs >= clickTime) {
        if (!best) best = seg
        else if (seg.id === selectedSegmentId) best = seg
      }
    }
    if (best) onSegmentSelect(best.id)
  }

  // Wheel-to-zoom must call preventDefault, which React's (passive) onWheel cannot do
  // without a console error and the page scrolling anyway — attach a non-passive
  // native listener instead.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    function onWheel(e: WheelEvent) {
      e.preventDefault()
      const rect = canvas!.getBoundingClientRect()
      const xRatio = (e.clientX - rect.left) / rect.width
      const cursorTime = clampedOffset + xRatio * windowDuration

      const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2
      const newZoom = Math.max(1, Math.min(20, zoom * factor))
      const newWindowDuration = totalDuration / newZoom
      // Keep cursorTime fixed: newOffset + xRatio * newWindowDuration = cursorTime
      const newOffset = cursorTime - xRatio * newWindowDuration

      setZoom(newZoom)
      setOffsetSecs(Math.max(0, Math.min(newOffset, totalDuration - newWindowDuration)))
    }

    canvas.addEventListener('wheel', onWheel, { passive: false })
    return () => canvas.removeEventListener('wheel', onWheel)
  }, [zoom, clampedOffset, windowDuration, totalDuration])

  if (segments.length === 0 || totalDuration === 0) return null

  return (
    <canvas
      ref={canvasRef}
      width={800}
      height={CANVAS_HEIGHT}
      style={{ height: CANVAS_HEIGHT, width: '100%', cursor: 'pointer', display: 'block' }}
      onClick={handleClick}
    />
  )
}
