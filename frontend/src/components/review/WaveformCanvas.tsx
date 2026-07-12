import { useEffect, useRef } from 'react'
import { useTheme } from '../../hooks/useTheme'

interface WaveformCanvasProps {
  /** Decoded once by the parent and shared with the audio player so the segment
   *  audio is downloaded only a single time. */
  audioBlob: Blob | null
  currentTime: number
  duration: number
  onSeek: (time: number) => void
  showSpectrogram?: boolean
}

const CANVAS_WIDTH = 600
const CANVAS_HEIGHT = 80
const FFT_SIZE = 256

const COLORS = {
  light: { bg: '#f9fafb', waveform: '#6366f1', empty: '#f3f4f6', emptyText: '#9ca3af' },
  dark: { bg: '#111827', waveform: '#818cf8', empty: '#1f2937', emptyText: '#6b7280' },
}

// In-place iterative radix-2 Cooley–Tukey FFT.
function fft(re: Float32Array, im: Float32Array) {
  const n = re.length
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1
    for (; j & bit; bit >>= 1) j ^= bit
    j ^= bit
    if (i < j) {
      const tr = re[i]; re[i] = re[j]; re[j] = tr
      const ti = im[i]; im[i] = im[j]; im[j] = ti
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len
    const wr = Math.cos(ang)
    const wi = Math.sin(ang)
    for (let i = 0; i < n; i += len) {
      let cwr = 1
      let cwi = 0
      for (let k = 0; k < len / 2; k++) {
        const half = i + k + len / 2
        const vr = re[half] * cwr - im[half] * cwi
        const vi = re[half] * cwi + im[half] * cwr
        const ur = re[i + k]
        const ui = im[i + k]
        re[i + k] = ur + vr
        im[i + k] = ui + vi
        re[half] = ur - vr
        im[half] = ui - vi
        const ncwr = cwr * wr - cwi * wi
        cwi = cwr * wi + cwi * wr
        cwr = ncwr
      }
    }
  }
}

// Simple blue→green→yellow intensity colormap (intensity in [0,1]).
function colormap(t: number): [number, number, number] {
  const v = Math.min(1, Math.max(0, t))
  if (v < 0.5) {
    const u = v / 0.5
    return [Math.round(20 + u * 20), Math.round(30 + u * 160), Math.round(120 + u * 80)]
  }
  const u = (v - 0.5) / 0.5
  return [Math.round(40 + u * 215), Math.round(190 + u * 60), Math.round(200 - u * 160)]
}

export function WaveformCanvas({ audioBlob, currentTime, duration, onSeek, showSpectrogram }: WaveformCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const samplesRef = useRef<Float32Array | null>(null)
  const loadingRef = useRef(false)
  // Incremented on every blob change so a slow decode from a previous segment can't
  // overwrite the current one (stale-response guard).
  const tokenRef = useRef(0)
  const { resolved } = useTheme()
  const colors = COLORS[resolved]

  function drawPlayhead(ctx: CanvasRenderingContext2D) {
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

  function drawWaveform(ctx: CanvasRenderingContext2D, samples: Float32Array) {
    ctx.fillStyle = colors.bg
    ctx.fillRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)

    const mid = CANVAS_HEIGHT / 2
    const samplesPerPixel = Math.max(1, Math.floor(samples.length / CANVAS_WIDTH))

    ctx.strokeStyle = colors.waveform
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

      ctx.beginPath()
      ctx.moveTo(x + 0.5, mid + min * mid)
      ctx.lineTo(x + 0.5, mid - max * mid)
      ctx.stroke()
    }
  }

  function drawSpectrogram(ctx: CanvasRenderingContext2D, samples: Float32Array) {
    if (samples.length < FFT_SIZE) {
      drawWaveform(ctx, samples)
      return
    }

    const numBins = FFT_SIZE / 2
    const hop = Math.max(1, Math.floor((samples.length - FFT_SIZE) / (CANVAS_WIDTH - 1)))

    // Precompute all column magnitudes and the global max for log normalisation.
    const columns: Float32Array[] = []
    let maxMag = 1e-9
    for (let c = 0; c < CANVAS_WIDTH; c++) {
      const start = c * hop
      const re = new Float32Array(FFT_SIZE)
      const im = new Float32Array(FFT_SIZE)
      for (let i = 0; i < FFT_SIZE; i++) {
        const idx = start + i
        const sample = idx < samples.length ? samples[idx] : 0
        // Hann window
        const w = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (FFT_SIZE - 1))
        re[i] = sample * w
      }
      fft(re, im)
      const mags = new Float32Array(numBins)
      for (let b = 0; b < numBins; b++) {
        const mag = Math.sqrt(re[b] * re[b] + im[b] * im[b])
        mags[b] = mag
        if (mag > maxMag) maxMag = mag
      }
      columns.push(mags)
    }

    const logMax = Math.log(1 + maxMag)
    const image = ctx.createImageData(CANVAS_WIDTH, CANVAS_HEIGHT)
    const data = image.data
    for (let x = 0; x < CANVAS_WIDTH; x++) {
      const mags = columns[x]
      for (let y = 0; y < CANVAS_HEIGHT; y++) {
        // Low frequencies at the bottom of the canvas.
        const bin = Math.floor((1 - y / (CANVAS_HEIGHT - 1)) * (numBins - 1))
        const intensity = Math.log(1 + mags[bin]) / logMax
        const [r, g, b] = colormap(intensity)
        const off = (y * CANVAS_WIDTH + x) * 4
        data[off] = r
        data[off + 1] = g
        data[off + 2] = b
        data[off + 3] = 255
      }
    }
    ctx.putImageData(image, 0, 0)
  }

  function draw() {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    ctx.clearRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)

    const samples = samplesRef.current
    if (!samples) {
      ctx.fillStyle = colors.empty
      ctx.fillRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)
      ctx.fillStyle = colors.emptyText
      ctx.font = '13px sans-serif'
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillText(loadingRef.current ? 'Loading waveform…' : '', CANVAS_WIDTH / 2, CANVAS_HEIGHT / 2)
      return
    }

    if (showSpectrogram) {
      drawSpectrogram(ctx, samples)
    } else {
      drawWaveform(ctx, samples)
    }
    drawPlayhead(ctx)
  }

  // Decode the shared blob (no network — the parent already downloaded it).
  useEffect(() => {
    samplesRef.current = null
    loadingRef.current = !!audioBlob
    draw()
    if (!audioBlob) return

    const token = ++tokenRef.current
    let audioCtx: AudioContext | null = null
    audioBlob
      .arrayBuffer()
      .then((buf) => {
        audioCtx = new AudioContext()
        return audioCtx.decodeAudioData(buf)
      })
      .then((decoded) => {
        void audioCtx?.close()
        if (token !== tokenRef.current) return // a newer segment superseded this decode
        samplesRef.current = decoded.getChannelData(0)
        loadingRef.current = false
        draw()
      })
      .catch(() => {
        void audioCtx?.close()
        if (token !== tokenRef.current) return
        loadingRef.current = false
        draw()
      })
  }, [audioBlob]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    draw()
  }, [currentTime, duration, showSpectrogram, resolved]) // eslint-disable-line react-hooks/exhaustive-deps

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
      className="w-full rounded cursor-pointer bg-gray-50 dark:bg-gray-900"
      style={{ height: CANVAS_HEIGHT }}
    />
  )
}
