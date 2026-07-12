import type {
  CreateProjectRequest,
  PatchProjectRequest,
  ProjectDetail,
  ProjectSummary,
  PatchSegmentRequest,
  BulkSegmentRequest,
  BulkFilter,
  GetSegmentsParams,
  PaginatedSegments,
  Segment,
  Job,
  ScoutStatus,
  Model,
  CreateModelRequest,
  Preview,
  CreatePreviewRequest,
} from '../types/api'

// When VITE_API_URL is unset (the default in the shipped compose), API calls go
// to a same-origin /api path, which the frontend's vite server proxies to the
// orchestrator (see vite.config.ts). Same-origin means no CORS and no mixed
// content when the UI is served over https behind a reverse proxy.
const BASE_URL = (import.meta.env.VITE_API_URL as string | undefined) || '/api'

// ---- Error type ----

export class ApiError extends Error {
  error: string
  detail: unknown

  constructor(error: string, message: string, detail: unknown) {
    super(message)
    this.name = 'ApiError'
    this.error = error
    this.detail = detail
  }
}

// ---- Internal helpers ----

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${BASE_URL}${path}`
  const res = await fetch(url, init)

  if (!res.ok) {
    let body: { error?: string; message?: string; detail?: unknown } = {}
    try {
      body = (await res.json()) as typeof body
    } catch {
      // ignore parse error, fall through to generic message
    }
    throw new ApiError(
      body.error ?? 'unknown_error',
      body.message ?? `HTTP ${res.status}`,
      body.detail ?? null,
    )
  }

  // 204 No Content
  if (res.status === 204) {
    return undefined as unknown as T
  }

  return res.json() as Promise<T>
}

function toQueryString(params: Record<string, unknown>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null)
  if (entries.length === 0) return ''
  return '?' + entries.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`).join('&')
}

// ---- Projects ----

export function getProjects(): Promise<{ projects: ProjectSummary[] }> {
  return request('/projects')
}

export function createProject(req: CreateProjectRequest): Promise<{ id: string; name: string; status: string }> {
  return request('/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
}

export function getProject(projectId: string): Promise<ProjectDetail> {
  return request(`/projects/${projectId}`)
}

export function patchProject(projectId: string, req: PatchProjectRequest): Promise<ProjectDetail> {
  return request(`/projects/${projectId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
}

export function deleteProject(projectId: string, confirm: boolean): Promise<{ deleted: boolean }> {
  return request(`/projects/${projectId}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm }),
  })
}

// ---- Uploads (XHR for progress) ----

// fetch() can't report upload progress, so file uploads go through XMLHttpRequest,
// which exposes upload.onprogress. Error handling mirrors request(): parse the
// flat {error, message, detail} body and throw ApiError.
function uploadWithProgress<T>(
  path: string,
  file: File,
  onProgress?: (fraction: number) => void,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const formData = new FormData()
    formData.append('file', file)

    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${BASE_URL}${path}`)

    xhr.upload.onprogress = (e) => {
      if (onProgress && e.lengthComputable) {
        onProgress(e.total > 0 ? e.loaded / e.total : 0)
      }
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve((xhr.responseText ? JSON.parse(xhr.responseText) : undefined) as T)
        } catch {
          reject(new ApiError('parse_error', 'Malformed response from server.', null))
        }
        return
      }
      let body: { error?: string; message?: string; detail?: unknown } = {}
      try {
        body = JSON.parse(xhr.responseText) as typeof body
      } catch {
        // fall through to generic message
      }
      reject(new ApiError(
        body.error ?? 'unknown_error',
        body.message ?? `HTTP ${xhr.status}`,
        body.detail ?? null,
      ))
    }

    xhr.onerror = () =>
      reject(new ApiError('network_error', 'Upload failed — the connection was interrupted.', null))
    xhr.onabort = () =>
      reject(new ApiError('aborted', 'Upload cancelled.', null))

    xhr.send(formData)
  })
}

// ---- Sources ----

export function uploadSource(
  projectId: string,
  file: File,
  onProgress?: (fraction: number) => void,
): Promise<{ id: string; filename: string; status: string }> {
  return uploadWithProgress(`/projects/${projectId}/sources`, file, onProgress)
}

export function deleteSource(
  projectId: string,
  sourceId: string,
  confirm: boolean,
): Promise<{ deleted_segment_count: number; deleted_approved_count: number }> {
  return request(`/projects/${projectId}/sources/${sourceId}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm }),
  })
}

// ---- Reference ----

export function uploadReference(
  projectId: string,
  file: File,
  onProgress?: (fraction: number) => void,
): Promise<{ reference_path: string; duration_secs: number }> {
  return uploadWithProgress(`/projects/${projectId}/reference`, file, onProgress)
}

// ---- Reference: diarise + pick (scout) ----

export function startScout(
  projectId: string,
  sourceId: string,
  expectedSpeakerCount?: number,
): Promise<{ job_id: string; type: string }> {
  return request(`/projects/${projectId}/reference/scout`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source_id: sourceId,
      ...(expectedSpeakerCount != null ? { expected_speaker_count: expectedSpeakerCount } : {}),
    }),
  })
}

export function getScoutStatus(projectId: string): Promise<ScoutStatus> {
  return request(`/projects/${projectId}/reference/scout`)
}

export function getScoutSampleUrl(projectId: string, speakerLabel: string, index: number): string {
  return `${BASE_URL}/projects/${projectId}/reference/scout/samples/${speakerLabel}/${index}`
}

// The assembled-reference montage for a candidate — what selecting this speaker
// would produce (included turns, longest-first, capped at 30s). `excludedIndices`
// mirrors the card's exclusion ticks so the preview stays in sync with curation.
export function getScoutPreviewUrl(
  projectId: string,
  speakerLabel: string,
  excludedIndices: number[] = [],
): string {
  const qs = excludedIndices.length
    ? '?' + excludedIndices.map((i) => `exclude=${encodeURIComponent(i)}`).join('&')
    : ''
  return `${BASE_URL}/projects/${projectId}/reference/scout/preview/${speakerLabel}${qs}`
}

export function selectScoutSpeaker(
  projectId: string,
  speakerLabel: string,
  excludedIndices: number[] = [],
): Promise<{ reference_path: string; duration_secs: number }> {
  return request(`/projects/${projectId}/reference/scout/select`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ speaker_label: speakerLabel, excluded_indices: excludedIndices }),
  })
}

// ---- Pipeline ----

export function startPipeline(projectId: string): Promise<{ enqueued_jobs: Job[] }> {
  return request(`/projects/${projectId}/pipeline/start`, {
    method: 'POST',
  })
}

export function continuePipeline(projectId: string): Promise<{ enqueued_jobs: Job[] }> {
  return request(`/projects/${projectId}/pipeline/continue`, {
    method: 'POST',
  })
}

export function reprocessSource(
  projectId: string,
  sourceId: string,
  steps: string[],
  params?: Record<string, unknown>,
  confirm?: boolean,
): Promise<unknown> {
  return request(`/projects/${projectId}/sources/${sourceId}/reprocess`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ steps, params, confirm }),
  })
}

// ---- Transcription ----

export function runTranscription(projectId: string): Promise<unknown> {
  return request(`/projects/${projectId}/transcription/run`, {
    method: 'POST',
  })
}

export function rerunSegmentTranscription(projectId: string, segmentId: string): Promise<unknown> {
  return request(`/projects/${projectId}/segments/${segmentId}/transcription/rerun`, {
    method: 'POST',
  })
}

// ---- Segments ----

export function getSegments(
  projectId: string,
  params: GetSegmentsParams,
  signal?: AbortSignal,
): Promise<PaginatedSegments> {
  return request(
    `/projects/${projectId}/segments${toQueryString(params as Record<string, unknown>)}`,
    { signal },
  )
}

export function getSegmentsCount(projectId: string, filter: BulkFilter): Promise<{ total: number }> {
  return request(
    `/projects/${projectId}/segments${toQueryString({ ...(filter as Record<string, unknown>), count_only: true })}`,
  )
}

export function getSegmentAudioUrl(projectId: string, segmentId: string): string {
  return `${BASE_URL}/projects/${projectId}/segments/${segmentId}/audio`
}

export function patchSegment(
  projectId: string,
  segmentId: string,
  req: PatchSegmentRequest,
): Promise<Segment> {
  return request(`/projects/${projectId}/segments/${segmentId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
}

export function bulkSegmentAction(
  projectId: string,
  req: BulkSegmentRequest,
): Promise<{ affected_count: number; skipped_no_transcript?: number }> {
  return request(`/projects/${projectId}/segments/bulk`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
}

// ---- Export ----

export interface EnqueuedJob {
  id: string
  type: string
  segment_count?: number
}

export function triggerExport(projectId: string): Promise<{ enqueued_job: EnqueuedJob }> {
  return request(`/projects/${projectId}/export`, {
    method: 'POST',
  })
}

export function getExportDownloadUrl(projectId: string): string {
  return `${BASE_URL}/projects/${projectId}/export/download`
}

// ---- Models (v1.5) ----

export function createModel(
  projectId: string,
  body: CreateModelRequest,
): Promise<{ model: Model; enqueued_jobs: EnqueuedJob[] }> {
  return request(`/projects/${projectId}/models`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function getModels(projectId: string): Promise<{ models: Model[] }> {
  return request(`/projects/${projectId}/models`)
}

export function deleteModel(projectId: string, modelId: string): Promise<void> {
  return request(`/projects/${projectId}/models/${modelId}`, {
    method: 'DELETE',
  })
}

// ---- Previews (v1.5) ----

export function createPreview(
  projectId: string,
  body: CreatePreviewRequest,
): Promise<{ enqueued_job: EnqueuedJob }> {
  return request(`/projects/${projectId}/previews`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function getPreviews(projectId: string): Promise<{ previews: Preview[] }> {
  return request(`/projects/${projectId}/previews`)
}

export function getPreviewAudioUrl(projectId: string, previewId: string): string {
  return `${BASE_URL}/projects/${projectId}/previews/${previewId}/audio`
}
