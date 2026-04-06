import { useEffect, useRef } from 'react'

interface WaveformCanvasProps {
  audioUrl: string | null
  currentTime: number
  duration: number
  onSeek: (time: number) => void
  showSpectrogram?: boolean
}

const CANVAS_WIDTH = 600
const CANVAS_HEIGHT = 80

export function WaveformCanvas({ audioUrl, currentTime, duration, onSeek }: WaveformCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const samplesRef = useRef<Float32Array | null>(null)
  const urlRef = useRef<string | null>(null)
  const loadingRef = useRef(false)

  function draw() {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    ctx.clearRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)

    const samples = samplesRef.current

    if (!samples) {
      // Loading placeholder
      ctx.fillStyle = '#f3f4f6'
      ctx.fillRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)
      ctx.fillStyle = '#9ca3af'
      ctx.font = '13px sans-serif'
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillText(loadingRef.current ? 'Loading waveform…' : '', CANVAS_WIDTH / 2, CANVAS_HEIGHT / 2)
      return
    }

    // Draw background
    ctx.fillStyle = '#f9fafb'
    ctx.fillRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)

    // Draw waveform
    const mid = CANVAS_HEIGHT / 2
    const samplesPerPixel = Math.max(1, Math.floor(samples.length / CANVAS_WIDTH))

    ctx.strokeStyle = '#6366f1' // indigo-500
    ctx.lineWidth = 1

    for (let x = 0; x < CANVAS_WIDTH; x++) {
      const start = x * samplesPerPixel
      const end = Math.min(start + samplesPerPixel, samples.length)

      let min = 0
      let max = 0
      for (let i = start; i < end; i++) {
        const s = samples[i]
        if (s < min) min = s
        if (s > max) max = s
      }

      const yMin = mid + min * mid
      const yMax = mid - max * mid

      ctx.beginPath()
      ctx.moveTo(x + 0.5, yMin)
      ctx.lineTo(x + 0.5, yMax)
      ctx.stroke()
    }

    // Draw playhead
    if (duration > 0) {
      const x = Math.round((currentTime / duration) * CANVAS_WIDTH)
      ctx.strokeStyle = '#ef4444' // red-500
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.moveTo(x, 0)
      ctx.lineTo(x, CANVAS_HEIGHT)
      ctx.stroke()
    }
  }

  useEffect(() => {
    if (!audioUrl || audioUrl === urlRef.current) return
    urlRef.current = audioUrl
    samplesRef.current = null
    loadingRef.current = true
    draw()

    const audioCtx = new AudioContext()
    fetch(audioUrl)
      .then(r => r.arrayBuffer())
      .then(buf => audioCtx.decodeAudioData(buf))
      .then(decoded => {
        samplesRef.current = decoded.getChannelData(0)
        loadingRef.current = false
        draw()
        void audioCtx.close()
      })
      .catch(() => {
        loadingRef.current = false
        void audioCtx.close()
        draw()
      })
  }, [audioUrl]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    draw()
  }, [currentTime, duration]) // eslint-disable-line react-hooks/exhaustive-deps

  function handleClick(e: React.MouseEvent<HTMLCanvasElement>) {
    const canvas = canvasRef.current
    if (!canvas || duration <= 0) return
    const rect = canvas.getBoundingClientRect()
    const x = e.clientX - rect.left
    const ratio = x / rect.width
    onSeek(ratio * duration)
  }

  return (
    <canvas
      ref={canvasRef}
      width={CANVAS_WIDTH}
      height={CANVAS_HEIGHT}
      onClick={handleClick}
      className="w-full rounded cursor-pointer bg-gray-50"
      style={{ height: CANVAS_HEIGHT }}
    />
  )
}
