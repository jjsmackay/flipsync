import { useState, useEffect, useRef, useCallback } from 'react'

interface UseAudioResult {
  isPlaying: boolean
  currentTime: number
  duration: number
  playbackRate: number
  play: () => void
  pause: () => void
  toggle: () => void
  seek: (time: number) => void
  setPlaybackRate: (rate: number) => void
  restart: () => void
}

export function useAudio(url: string | null): UseAudioResult {
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const [isPlaying, setIsPlaying] = useState(false)
  const [currentTime, setCurrentTime] = useState(0)
  const [duration, setDuration] = useState(0)
  const [playbackRate, setPlaybackRateState] = useState(1)

  // Create / replace audio element when URL changes
  useEffect(() => {
    const audio = new Audio()
    audioRef.current = audio

    if (url) {
      audio.src = url
    }

    const handleTimeUpdate = () => setCurrentTime(audio.currentTime)
    const handleLoadedMetadata = () => setDuration(audio.duration)
    const handleEnded = () => setIsPlaying(false)
    const handlePlay = () => setIsPlaying(true)
    const handlePause = () => setIsPlaying(false)

    audio.addEventListener('timeupdate', handleTimeUpdate)
    audio.addEventListener('loadedmetadata', handleLoadedMetadata)
    audio.addEventListener('ended', handleEnded)
    audio.addEventListener('play', handlePlay)
    audio.addEventListener('pause', handlePause)

    return () => {
      audio.pause()
      audio.removeEventListener('timeupdate', handleTimeUpdate)
      audio.removeEventListener('loadedmetadata', handleLoadedMetadata)
      audio.removeEventListener('ended', handleEnded)
      audio.removeEventListener('play', handlePlay)
      audio.removeEventListener('pause', handlePause)
      audioRef.current = null
    }
  }, [url])

  const play = useCallback(() => {
    audioRef.current?.play().catch(() => {
      // Silently ignore AbortError from rapid play/pause cycles
    })
  }, [])

  const pause = useCallback(() => {
    audioRef.current?.pause()
  }, [])

  const toggle = useCallback(() => {
    if (audioRef.current?.paused ?? true) {
      play()
    } else {
      pause()
    }
  }, [play, pause])

  const seek = useCallback((time: number) => {
    if (audioRef.current) {
      audioRef.current.currentTime = time
    }
  }, [])

  const setPlaybackRate = useCallback((rate: number) => {
    if (audioRef.current) {
      audioRef.current.playbackRate = rate
    }
    setPlaybackRateState(rate)
  }, [])

  const restart = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.currentTime = 0
      void audioRef.current.play().catch(() => undefined)
    }
  }, [])

  return {
    isPlaying,
    currentTime,
    duration,
    playbackRate,
    play,
    pause,
    toggle,
    seek,
    setPlaybackRate,
    restart,
  }
}
