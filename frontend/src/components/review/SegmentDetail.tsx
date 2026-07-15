import { useState, useEffect, useRef } from 'react'
import type { Segment, SegmentStatus } from '../../types/api'
import { adjustSegmentBoundaries, getSegmentAudioUrl, patchSegment, rerunSegmentTranscription } from '../../api/client'
import { useAudio } from '../../hooks/useAudio'
import { ConfidenceBadge } from '../ui/ConfidenceBadge'
import { StatusBadge } from '../ui/StatusBadge'
import { WaveformCanvas } from './WaveformCanvas'
import { AudioControls } from './AudioControls'
import { formatTimestamp, formatSecondsPrecise } from '../../utils/format'
import { errorMessage } from '../../utils/errors'

interface SegmentDetailProps {
  projectId: string
  segment: Segment
  onStatusChange: (id: string, status: SegmentStatus) => void
  onTranscriptChange: (id: string, transcript: string | null) => void
  onSegmentUpdate: (segment: Segment) => void
  inStitch: boolean
  onToggleStitch: () => void
  onFocusChange: (focused: boolean) => void
  showSpectrogram: boolean
  onSpectrogramToggle: () => void
  autoPlay: boolean
}

const MATCH_TOOLTIP =
  'Speaker match: cosine similarity between this segment and your reference clip (0–1). Higher means more likely to be the target speaker.'
const TRANSCRIPT_TOOLTIP =
  'Transcript confidence: the average word probability reported by Whisper for this segment (0–1). Lower values often mean unclear speech.'

const PLAYBACK_RATES = [0.75, 1.0, 1.25, 1.5]

export function SegmentDetail({
  projectId,
  segment,
  onStatusChange,
  onTranscriptChange,
  onSegmentUpdate,
  inStitch,
  onToggleStitch,
  onFocusChange,
  showSpectrogram,
  onSpectrogramToggle,
  autoPlay,
}: SegmentDetailProps) {
  // Cache-bust on duration so a boundary re-cut (same URL, new bytes) reloads
  // the player and waveform instead of serving the stale cached WAV.
  const audioApiUrl = `${getSegmentAudioUrl(projectId, segment.id)}?v=${segment.duration_secs}`
  const [audioBlob, setAudioBlob] = useState<Blob | null>(null)
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const [audioError, setAudioError] = useState<string | null>(null)
  const audio = useAudio(objectUrl)

  const [isEditing, setIsEditing] = useState(false)
  const [editedTranscript, setEditedTranscript] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [retranscribing, setRetranscribing] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Trim/extend boundary nudge: each box is a signed amount in seconds applied
  // to that edge (+ extends outward = more audio, − trims inward).
  const [startNudge, setStartNudge] = useState('')
  const [endNudge, setEndNudge] = useState('')
  const [adjusting, setAdjusting] = useState(false)

  async function applyBoundaries() {
    const startAmt = parseFloat(startNudge) || 0
    const endAmt = parseFloat(endNudge) || 0
    if (!startAmt && !endAmt) return
    setAdjusting(true)
    setError(null)
    try {
      const req: { start_secs?: number; end_secs?: number } = {}
      if (startAmt) req.start_secs = Math.max(0, segment.start_secs - startAmt)
      if (endAmt) req.end_secs = segment.end_secs + endAmt
      const updated = await adjustSegmentBoundaries(projectId, segment.id, req)
      onSegmentUpdate(updated)
      setStartNudge('')
      setEndNudge('')
    } catch (e) {
      setError(errorMessage(e, 'Adjust failed'))
    } finally {
      setAdjusting(false)
    }
  }

  async function handleRetranscribe() {
    setError(null)
    setRetranscribing(true)
    try {
      await rerunSegmentTranscription(projectId, segment.id)
      // The job runs asynchronously; the new transcript lands on the next
      // queue refresh. Leave the button disabled to signal it's queued.
    } catch (e) {
      setError(errorMessage(e, 'Re-transcribe failed'))
      setRetranscribing(false)
    }
  }

  // Clear the "queued" state once the new transcript arrives (or on navigation
  // to another segment).
  useEffect(() => {
    setRetranscribing(false)
  }, [segment.id, segment.transcript])

  // Download the segment audio once, as a blob, and hand the same bytes to both the
  // audio player (via object URL) and the waveform/spectrogram (via the blob). The
  // object URL is revoked when the segment changes or the panel unmounts.
  useEffect(() => {
    let cancelled = false
    let createdUrl: string | null = null
    setAudioBlob(null)
    setObjectUrl(null)
    setAudioError(null)
    fetch(audioApiUrl)
      .then((r) => {
        // A non-2xx here returns the JSON error body — never hand that to the
        // audio element or waveform decoder.
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.blob()
      })
      .then((blob) => {
        if (cancelled) return
        createdUrl = URL.createObjectURL(blob)
        setAudioBlob(blob)
        setObjectUrl(createdUrl)
      })
      .catch(() => {
        if (cancelled) return
        setAudioError('Audio unavailable — this segment may have been re-cut or its source reprocessed.')
      })
    return () => {
      cancelled = true
      if (createdUrl) URL.revokeObjectURL(createdUrl)
    }
  }, [audioApiUrl])

  // Auto-play when a new segment's audio is ready, if the header toggle is on.
  useEffect(() => {
    if (autoPlay && objectUrl) {
      audio.play()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [objectUrl, autoPlay])

  // Reset state when segment changes
  useEffect(() => {
    setIsEditing(false)
    setEditedTranscript('')
    setError(null)
    setSaving(false)
    setStartNudge('')
    setEndNudge('')
  }, [segment.id])

  // Keyboard shortcuts (Space/R/E/[/]) — only when not editing
  useEffect(() => {
    if (isEditing) return
    function onKey(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement).tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      switch (e.key) {
        case ' ':
          e.preventDefault()
          audio.toggle()
          break
        case 'r':
        case 'R':
          e.preventDefault()
          audio.restart()
          break
        case 'e':
        case 'E':
          e.preventDefault()
          startEditing()
          break
        case '[': {
          e.preventDefault()
          const idx = PLAYBACK_RATES.indexOf(audio.playbackRate)
          const prev = idx <= 0 ? PLAYBACK_RATES[PLAYBACK_RATES.length - 1] : PLAYBACK_RATES[idx - 1]
          audio.setPlaybackRate(prev)
          break
        }
        case ']': {
          e.preventDefault()
          const idx = PLAYBACK_RATES.indexOf(audio.playbackRate)
          const next = idx >= PLAYBACK_RATES.length - 1 ? PLAYBACK_RATES[0] : PLAYBACK_RATES[idx + 1]
          audio.setPlaybackRate(next)
          break
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isEditing, audio])

  function startEditing() {
    const current = segment.transcript_edited ?? segment.transcript ?? ''
    setEditedTranscript(current)
    setIsEditing(true)
    onFocusChange(false)
    setTimeout(() => textareaRef.current?.focus(), 0)
  }

  function cancelEditing() {
    setIsEditing(false)
    setError(null)
    onFocusChange(true)
  }

  async function saveTranscript() {
    if (saving) return
    setSaving(true)
    setError(null)
    try {
      await patchSegment(projectId, segment.id, { transcript_edited: editedTranscript })
      onTranscriptChange(segment.id, editedTranscript)
      setIsEditing(false)
      onFocusChange(true)
    } catch (e) {
      setError(errorMessage(e, 'Save failed'))
    } finally {
      setSaving(false)
    }
  }

  function handleTextareaKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Escape') {
      e.preventDefault()
      cancelEditing()
    } else if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void saveTranscript()
    }
  }

  async function handleStatusAction(status: SegmentStatus) {
    setError(null)
    try {
      await patchSegment(projectId, segment.id, { status })
      onStatusChange(segment.id, status)
    } catch (e) {
      setError(errorMessage(e, 'Action failed'))
    }
  }

  const displayTranscript = segment.transcript_edited ?? segment.transcript

  return (
    <div className="flex flex-col gap-3 p-4 h-full overflow-y-auto">
      {/* Header */}
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2 flex-wrap">
          <StatusBadge status={segment.status} />
          <ConfidenceBadge value={segment.match_confidence} label="match" title={MATCH_TOOLTIP} />
          {segment.transcript_confidence !== null && (
            <ConfidenceBadge value={segment.transcript_confidence} label="transcript" title={TRANSCRIPT_TOOLTIP} />
          )}
          {segment.clipping_warning && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-orange-100 dark:bg-orange-900/40 text-orange-700 dark:text-orange-300">
              ⚡ Clipping
            </span>
          )}
        </div>
        <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
          {segment.source_filename} &nbsp;·&nbsp; {formatTimestamp(segment.start_secs)} – {formatTimestamp(segment.end_secs)}
          &nbsp;({formatSecondsPrecise(segment.duration_secs)})
        </p>
        {segment.speaker_match_confidence != null && (
          <p
            className="text-xs text-gray-500 dark:text-gray-400"
            title="Cluster score: how well this segment's diarisation cluster matches the reference overall (secondary signal to the per-segment match)."
          >
            Cluster score: {segment.speaker_match_confidence.toFixed(2)}
          </p>
        )}
        {segment.flags && segment.flags.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-1">
            {segment.flags.map((flag) => {
              const isCleanupError = flag.startsWith('cleanup_error')
              return (
                <span
                  key={flag}
                  className={`inline-flex items-center px-2 py-0.5 rounded text-xs ${
                    isCleanupError
                      ? 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400'
                      : 'bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-400'
                  }`}
                  title={
                    flag === 'short_transcript'
                      ? 'Short segment: transcript confidence may be unreliable'
                      : flag === 'boundary_edited'
                        ? 'Boundaries were re-cut after transcription — re-transcribe if the words changed'
                        : isCleanupError
                          ? flag.replace('cleanup_error: ', '')
                          : flag
                  }
                >
                  {flag === 'short_transcript'
                    ? 'Short transcript'
                    : flag === 'boundary_edited'
                      ? 'Boundary edited'
                      : isCleanupError
                        ? 'Cleanup error'
                        : flag}
                </span>
              )
            })}
          </div>
        )}
      </div>

      {/* Waveform */}
      <div className="flex flex-col gap-1.5">
        {audioError ? (
          <p className="text-sm text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded px-3 py-2">
            {audioError}
          </p>
        ) : (
          <>
            <WaveformCanvas
              audioBlob={audioBlob}
              currentTime={audio.currentTime}
              duration={audio.duration}
              onSeek={audio.seek}
              showSpectrogram={showSpectrogram}
            />
            <AudioControls
              isPlaying={audio.isPlaying}
              currentTime={audio.currentTime}
              duration={audio.duration}
              playbackRate={audio.playbackRate}
              onToggle={audio.toggle}
              onRestart={audio.restart}
              onSpeedChange={audio.setPlaybackRate}
            />

            {/* Spectrogram toggle */}
            <button
              type="button"
              onClick={onSpectrogramToggle}
              className="self-start text-xs text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300 focus:outline-none"
            >
              {showSpectrogram ? 'Hide spectrogram' : 'Show spectrogram'}
            </button>

            {/* Trim / extend boundaries */}
            <div className="flex flex-wrap items-end gap-3 pt-1">
              <div className="flex flex-col gap-0.5">
                <label htmlFor="start-nudge" className="text-xs text-gray-500 dark:text-gray-400">
                  Start (s)
                </label>
                <input
                  id="start-nudge"
                  type="number"
                  step={0.05}
                  value={startNudge}
                  onChange={(e) => setStartNudge(e.target.value)}
                  placeholder="0"
                  className="w-20 border border-gray-300 dark:border-gray-600 rounded px-2 py-1 text-sm dark:bg-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-indigo-400"
                />
              </div>
              <div className="flex flex-col gap-0.5">
                <label htmlFor="end-nudge" className="text-xs text-gray-500 dark:text-gray-400">
                  End (s)
                </label>
                <input
                  id="end-nudge"
                  type="number"
                  step={0.05}
                  value={endNudge}
                  onChange={(e) => setEndNudge(e.target.value)}
                  placeholder="0"
                  className="w-20 border border-gray-300 dark:border-gray-600 rounded px-2 py-1 text-sm dark:bg-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-indigo-400"
                />
              </div>
              <button
                type="button"
                onClick={() => void applyBoundaries()}
                disabled={adjusting || (!parseFloat(startNudge) && !parseFloat(endNudge))}
                title="Re-cut this segment from the source audio. + extends the edge outward (recovers a clipped word), − trims it inward (drops bleed)."
                className="px-3 py-1 rounded text-xs bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-indigo-400"
              >
                {adjusting ? 'Re-cutting…' : 'Apply'}
              </button>
              <span className="text-xs text-gray-400 dark:text-gray-500">+ extends · − trims</span>
              <button
                type="button"
                onClick={onToggleStitch}
                title="Queue this segment to be concatenated with others into one clip."
                className={`ml-auto px-2 py-1 rounded text-xs focus:outline-none focus:ring-2 focus:ring-indigo-400 ${
                  inStitch
                    ? 'bg-indigo-600 text-white hover:bg-indigo-700'
                    : 'border border-indigo-300 dark:border-indigo-700 text-indigo-600 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-900/30'
                }`}
              >
                {inStitch ? '✓ In stitch' : '+ Stitch'}
              </button>
            </div>
          </>
        )}
      </div>

      {/* Transcript */}
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-gray-600 dark:text-gray-400 uppercase tracking-wide">Transcript</span>
          {!isEditing && (
            <div className="flex items-center gap-2">
              {segment.transcript_edited !== null && (
                <button
                  type="button"
                  onClick={async () => {
                    try {
                      await patchSegment(projectId, segment.id, { transcript_edited: null })
                      // Clear the edit locally so the UI falls back to the original transcript
                      // and the Undo button disappears (SC2).
                      onTranscriptChange(segment.id, null)
                    } catch (e) {
                      setError(errorMessage(e, 'Undo failed'))
                    }
                  }}
                  className="text-xs text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400 focus:outline-none"
                >
                  Undo edit
                </button>
              )}
              <button
                type="button"
                onClick={() => void handleRetranscribe()}
                disabled={retranscribing}
                title="Run transcription again for this segment (fills or replaces its transcript)."
                className="text-xs text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300 focus:outline-none disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {retranscribing ? 'Re-transcribing…' : 'Re-transcribe'}
              </button>
              <button
                type="button"
                onClick={startEditing}
                className="text-xs text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300 focus:outline-none"
              >
                Edit (E)
              </button>
            </div>
          )}
        </div>

        {isEditing ? (
          <div className="flex flex-col gap-1.5">
            <textarea
              ref={textareaRef}
              value={editedTranscript}
              onChange={e => setEditedTranscript(e.target.value)}
              onKeyDown={handleTextareaKeyDown}
              rows={4}
              className="w-full border border-indigo-400 dark:border-indigo-500 rounded px-2 py-1.5 text-sm text-gray-800 dark:text-gray-100 dark:bg-gray-900 resize-vertical focus:outline-none focus:ring-2 focus:ring-indigo-400"
            />
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => void saveTranscript()}
                disabled={saving}
                className="px-3 py-1 rounded text-xs bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-indigo-400"
              >
                {saving ? 'Saving…' : 'Save (Enter)'}
              </button>
              <button
                type="button"
                onClick={cancelEditing}
                className="px-3 py-1 rounded text-xs bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600 focus:outline-none focus:ring-2 focus:ring-indigo-400"
              >
                Cancel (Esc)
              </button>
            </div>
          </div>
        ) : (
          <p
            onClick={startEditing}
            className="text-sm text-gray-700 dark:text-gray-300 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/50 rounded px-1 py-0.5 min-h-[2.5rem]"
          >
            {displayTranscript !== null
              ? displayTranscript || <span className="italic text-gray-400 dark:text-gray-500">No transcript — click to add</span>
              : <span className="italic text-gray-400 dark:text-gray-500">Transcript pending</span>
            }
          </p>
        )}
      </div>

      {/* Error */}
      {error && (
        <p className="text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 rounded px-2 py-1">{error}</p>
      )}

      {/* Auto-approved chip */}
      {segment.status === 'auto_approved' && (
        <div
          className="mt-auto inline-flex w-fit items-center gap-1 px-2 py-1 rounded text-xs font-medium bg-teal-100 dark:bg-teal-900/40 text-teal-800 dark:text-teal-300 cursor-help"
          title="Approved automatically — speaker match and transcript confidence both cleared the project's auto-approve thresholds. Approve to confirm, or override."
        >
          Auto-approved
        </div>
      )}

      {/* Action buttons */}
      <div className={`flex gap-2 ${segment.status === 'auto_approved' ? '' : 'mt-auto'} pt-2 border-t border-gray-100 dark:border-gray-800`}>
        {segment.status === 'rejected' ? (
          // Misclick recovery: no A/M/X keyboard shortcut for this — a button-only
          // undo so a second misclick can't immediately re-trigger it.
          <button
            type="button"
            onClick={() => void handleStatusAction('pending')}
            className="flex-1 py-2 rounded text-sm font-medium bg-indigo-100 dark:bg-indigo-900/40 text-indigo-800 dark:text-indigo-300 hover:bg-indigo-200 dark:hover:bg-indigo-900/60 focus:outline-none focus:ring-2 focus:ring-indigo-400"
          >
            Un-reject (restore to pending)
          </button>
        ) : (
          <>
            <button
              type="button"
              onClick={() => void handleStatusAction('approved')}
              title={segment.clipping_warning ? 'Segment has clipping warning' : undefined}
              className="flex-1 py-2 rounded text-sm font-medium bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-300 hover:bg-green-200 dark:hover:bg-green-900/60 focus:outline-none focus:ring-2 focus:ring-green-400"
            >
              {segment.clipping_warning && '⚡ '}Approve (A)
            </button>
            <button
              type="button"
              onClick={() => void handleStatusAction('maybe')}
              className="flex-1 py-2 rounded text-sm font-medium bg-yellow-100 dark:bg-yellow-900/40 text-yellow-800 dark:text-yellow-300 hover:bg-yellow-200 dark:hover:bg-yellow-900/60 focus:outline-none focus:ring-2 focus:ring-yellow-400"
            >
              Maybe (M)
            </button>
            <button
              type="button"
              onClick={() => void handleStatusAction('rejected')}
              className="flex-1 py-2 rounded text-sm font-medium bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-300 hover:bg-red-200 dark:hover:bg-red-900/60 focus:outline-none focus:ring-2 focus:ring-red-400"
            >
              Reject (X)
            </button>
          </>
        )}
      </div>
    </div>
  )
}
