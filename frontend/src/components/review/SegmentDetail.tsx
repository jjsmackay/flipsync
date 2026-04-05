import { useState, useEffect, useRef } from 'react'
import type { Segment, SegmentStatus } from '../../types/api'
import { getSegmentAudioUrl, patchSegment } from '../../api/client'
import { useAudio } from '../../hooks/useAudio'
import { ConfidenceBadge } from '../ui/ConfidenceBadge'
import { StatusBadge } from '../ui/StatusBadge'
import { WaveformCanvas } from './WaveformCanvas'
import { AudioControls } from './AudioControls'

interface SegmentDetailProps {
  projectId: string
  segment: Segment
  onStatusChange: (id: string, status: SegmentStatus) => void
  onTranscriptChange: (id: string, transcript: string) => void
  onFocusChange: (focused: boolean) => void
  showSpectrogram: boolean
  onSpectrogramToggle: () => void
}

function formatTime(secs: number): string {
  const m = Math.floor(secs / 60)
  const s = (secs % 60).toFixed(1)
  return `${m}:${s.padStart(4, '0')}`
}

export function SegmentDetail({
  projectId,
  segment,
  onStatusChange,
  onTranscriptChange,
  onFocusChange,
  showSpectrogram,
  onSpectrogramToggle,
}: SegmentDetailProps) {
  const audioUrl = getSegmentAudioUrl(projectId, segment.id)
  const audio = useAudio(audioUrl)

  const [isEditing, setIsEditing] = useState(false)
  const [editedTranscript, setEditedTranscript] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Reset state when segment changes
  useEffect(() => {
    setIsEditing(false)
    setEditedTranscript('')
    setError(null)
    setSaving(false)
  }, [segment.id])

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
      setError(e instanceof Error ? e.message : 'Save failed')
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
      setError(e instanceof Error ? e.message : 'Action failed')
    }
  }

  const displayTranscript = segment.transcript_edited ?? segment.transcript

  return (
    <div className="flex flex-col gap-3 p-4 h-full overflow-y-auto">
      {/* Header */}
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2 flex-wrap">
          <StatusBadge status={segment.status} />
          <ConfidenceBadge value={segment.match_confidence} label="match" />
          {segment.transcript_confidence !== null && (
            <ConfidenceBadge value={segment.transcript_confidence} label="transcript" />
          )}
          {segment.clipping_warning && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-orange-100 text-orange-700">
              ⚡ Clipping
            </span>
          )}
        </div>
        <p className="text-xs text-gray-500 truncate">
          {segment.source_filename} &nbsp;·&nbsp; {formatTime(segment.start_secs)} – {formatTime(segment.end_secs)}
          &nbsp;({segment.duration_secs.toFixed(1)}s)
        </p>
      </div>

      {/* Waveform */}
      <div className="flex flex-col gap-1.5">
        <WaveformCanvas
          audioUrl={audioUrl}
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
          className="self-start text-xs text-indigo-600 hover:text-indigo-800 focus:outline-none"
        >
          {showSpectrogram ? 'Hide spectrogram' : 'Show spectrogram'}
        </button>
      </div>

      {/* Transcript */}
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-gray-600 uppercase tracking-wide">Transcript</span>
          {!isEditing && (
            <button
              type="button"
              onClick={startEditing}
              className="text-xs text-indigo-600 hover:text-indigo-800 focus:outline-none"
            >
              Edit (E)
            </button>
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
              className="w-full border border-indigo-400 rounded px-2 py-1.5 text-sm text-gray-800 resize-vertical focus:outline-none focus:ring-2 focus:ring-indigo-400"
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
                className="px-3 py-1 rounded text-xs bg-gray-100 text-gray-700 hover:bg-gray-200 focus:outline-none focus:ring-2 focus:ring-indigo-400"
              >
                Cancel (Esc)
              </button>
            </div>
          </div>
        ) : (
          <p
            onClick={startEditing}
            className="text-sm text-gray-700 cursor-pointer hover:bg-gray-50 rounded px-1 py-0.5 min-h-[2.5rem]"
          >
            {displayTranscript ?? <span className="italic text-gray-400">No transcript — click to add</span>}
          </p>
        )}
      </div>

      {/* Error */}
      {error && (
        <p className="text-xs text-red-600 bg-red-50 rounded px-2 py-1">{error}</p>
      )}

      {/* Action buttons */}
      <div className="flex gap-2 mt-auto pt-2 border-t border-gray-100">
        <button
          type="button"
          onClick={() => void handleStatusAction('approved')}
          className="flex-1 py-2 rounded text-sm font-medium bg-green-100 text-green-800 hover:bg-green-200 focus:outline-none focus:ring-2 focus:ring-green-400"
        >
          Approve (A)
        </button>
        <button
          type="button"
          onClick={() => void handleStatusAction('maybe')}
          className="flex-1 py-2 rounded text-sm font-medium bg-yellow-100 text-yellow-800 hover:bg-yellow-200 focus:outline-none focus:ring-2 focus:ring-yellow-400"
        >
          Maybe (M)
        </button>
        <button
          type="button"
          onClick={() => void handleStatusAction('rejected')}
          className="flex-1 py-2 rounded text-sm font-medium bg-red-100 text-red-800 hover:bg-red-200 focus:outline-none focus:ring-2 focus:ring-red-400"
        >
          Reject (X)
        </button>
      </div>
    </div>
  )
}
