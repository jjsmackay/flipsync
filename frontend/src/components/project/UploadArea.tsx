import { useRef, useState, DragEvent, ChangeEvent } from 'react'
import { uploadSource, uploadReference } from '../../api/client'

interface UploadAreaProps {
  projectId: string
  onUploaded: () => void
}

export function UploadArea({ projectId, onUploaded }: UploadAreaProps) {
  const [uploading, setUploading] = useState(false)
  const [uploadingName, setUploadingName] = useState<string | null>(null)
  // null = upload in flight but browser can't compute progress (indeterminate)
  const [progress, setProgress] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)

  const fileInputRef = useRef<HTMLInputElement>(null)
  const refInputRef = useRef<HTMLInputElement>(null)

  async function handleSourceFile(file: File) {
    setError(null)
    setUploading(true)
    setUploadingName(file.name)
    setProgress(0)
    try {
      await uploadSource(projectId, file, (f) => setProgress(f))
      onUploaded()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setUploading(false)
      setUploadingName(null)
      setProgress(null)
    }
  }

  async function handleReferenceFile(file: File) {
    setError(null)
    setUploading(true)
    setUploadingName(file.name)
    setProgress(0)
    try {
      await uploadReference(projectId, file, (f) => setProgress(f))
      onUploaded()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setUploading(false)
      setUploadingName(null)
      setProgress(null)
    }
  }

  function onDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault()
    setDragOver(false)
    const file = e.dataTransfer.files[0]
    if (file) handleSourceFile(file)
  }

  function onDragOver(e: DragEvent<HTMLDivElement>) {
    e.preventDefault()
    setDragOver(true)
  }

  function onDragLeave() {
    setDragOver(false)
  }

  function onFileChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (file) handleSourceFile(file)
    e.target.value = ''
  }

  function onRefChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (file) handleReferenceFile(file)
    e.target.value = ''
  }

  return (
    <div className="space-y-3">
      <div
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onClick={() => !uploading && fileInputRef.current?.click()}
        className={`border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors
          ${dragOver ? 'border-blue-400 bg-blue-50' : 'border-gray-300 hover:border-gray-400 hover:bg-gray-50'}
          ${uploading ? 'opacity-60 cursor-not-allowed' : ''}`}
      >
        {uploading ? (
          <div className="space-y-2">
            <p className="text-sm text-gray-600">
              {progress != null && progress >= 1 ? 'Finalising…' : 'Uploading…'}
              {progress != null && progress < 1 && (
                <span className="ml-1 font-mono text-gray-500">
                  {Math.round(progress * 100)}%
                </span>
              )}
            </p>
            {uploadingName && (
              <p className="text-xs text-gray-500 font-medium truncate">{uploadingName}</p>
            )}
            <div className="w-full h-1.5 bg-gray-200 rounded-full overflow-hidden">
              <div
                className={`h-full bg-blue-600 transition-[width] duration-150 ease-out
                  ${progress == null || progress >= 1 ? 'animate-pulse' : ''}`}
                style={{ width: `${Math.round((progress ?? 1) * 100)}%` }}
              />
            </div>
          </div>
        ) : (
          <div className="space-y-1">
            <p className="text-sm text-gray-600">
              Drag &amp; drop a video or audio file, or{' '}
              <span className="text-blue-600 font-medium">click to browse</span>
            </p>
            <p className="text-xs text-gray-400">Supports video and audio files</p>
          </div>
        )}
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept="video/*,audio/*"
        className="hidden"
        onChange={onFileChange}
        disabled={uploading}
      />

      <button
        onClick={() => !uploading && refInputRef.current?.click()}
        disabled={uploading}
        className="px-3 py-1.5 text-sm border border-gray-300 text-gray-700 rounded-lg
          hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
      >
        Upload reference clip
      </button>

      <input
        ref={refInputRef}
        type="file"
        accept="audio/*"
        className="hidden"
        onChange={onRefChange}
        disabled={uploading}
      />

      {error && (
        <p className="text-sm text-red-600">{error}</p>
      )}
    </div>
  )
}
