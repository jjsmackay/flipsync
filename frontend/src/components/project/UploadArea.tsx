import { useRef, useState, DragEvent, ChangeEvent } from 'react'
import { uploadSource } from '../../api/client'

interface UploadAreaProps {
  projectId: string
  onUploaded: () => void
  /** Render as a small "+ Add video" button instead of the full dropzone. */
  compact?: boolean
}

export function UploadArea({ projectId, onUploaded, compact = false }: UploadAreaProps) {
  const [uploading, setUploading] = useState(false)
  const [uploadingName, setUploadingName] = useState<string | null>(null)
  // null = upload in flight but browser can't compute progress (indeterminate)
  const [progress, setProgress] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)

  const fileInputRef = useRef<HTMLInputElement>(null)

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

  const fileInput = (
    <input
      ref={fileInputRef}
      type="file"
      accept="video/*,audio/*"
      className="hidden"
      onChange={onFileChange}
      disabled={uploading}
    />
  )

  if (compact) {
    return (
      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={() => !uploading && fileInputRef.current?.click()}
          disabled={uploading}
          className="px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 rounded-lg
            hover:bg-gray-50 dark:hover:bg-gray-700/50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {uploading
            ? `Uploading${progress != null && progress < 1 ? ` ${Math.round(progress * 100)}%` : '…'}`
            : '+ Add video'}
        </button>
        {uploading && uploadingName && (
          <span className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-[16rem]">{uploadingName}</span>
        )}
        {error && <span className="text-sm text-red-600 dark:text-red-400">{error}</span>}
        {fileInput}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onClick={() => !uploading && fileInputRef.current?.click()}
        className={`border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors
          ${dragOver ? 'border-blue-400 bg-blue-50 dark:bg-blue-900/20' : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500 hover:bg-gray-50 dark:hover:bg-gray-700/50'}
          ${uploading ? 'opacity-60 cursor-not-allowed' : ''}`}
      >
        {uploading ? (
          <div className="space-y-2">
            <p className="text-sm text-gray-600 dark:text-gray-400">
              {progress != null && progress >= 1 ? 'Finalising…' : 'Uploading…'}
              {progress != null && progress < 1 && (
                <span className="ml-1 font-mono text-gray-500 dark:text-gray-400">
                  {Math.round(progress * 100)}%
                </span>
              )}
            </p>
            {uploadingName && (
              <p className="text-xs text-gray-500 dark:text-gray-400 font-medium truncate">{uploadingName}</p>
            )}
            <div className="w-full h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
              <div
                className={`h-full bg-blue-600 transition-[width] duration-150 ease-out
                  ${progress == null || progress >= 1 ? 'animate-pulse' : ''}`}
                style={{ width: `${Math.round((progress ?? 1) * 100)}%` }}
              />
            </div>
          </div>
        ) : (
          <div className="space-y-1">
            <p className="text-sm text-gray-600 dark:text-gray-400">
              Drag &amp; drop a video or audio file, or{' '}
              <span className="text-blue-600 dark:text-blue-400 font-medium">click to browse</span>
            </p>
            <p className="text-xs text-gray-400 dark:text-gray-500">Supports video and audio files</p>
          </div>
        )}
      </div>

      {fileInput}

      {error && (
        <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
      )}
    </div>
  )
}
