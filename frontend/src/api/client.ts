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
} from '../types/api'

const BASE_URL = (import.meta.env.VITE_API_URL as string | undefined) || 'http://localhost:8000'

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
  return request(`/projects/${projectId}${toQueryString({ confirm })}`, {
    method: 'DELETE',
  })
}

// ---- Sources ----

export function uploadSource(
  projectId: string,
  file: File,
): Promise<{ id: string; filename: string; status: string }> {
  const formData = new FormData()
  formData.append('file', file)
  return request(`/projects/${projectId}/sources`, {
    method: 'POST',
    body: formData,
  })
}

export function deleteSource(
  projectId: string,
  sourceId: string,
  confirm: boolean,
): Promise<{ deleted_segment_count: number; deleted_approved_count: number }> {
  return request(`/projects/${projectId}/sources/${sourceId}${toQueryString({ confirm })}`, {
    method: 'DELETE',
  })
}

// ---- Reference ----

export function uploadReference(
  projectId: string,
  file: File,
): Promise<{ reference_path: string; duration_secs: number }> {
  const formData = new FormData()
  formData.append('file', file)
  return request(`/projects/${projectId}/reference`, {
    method: 'POST',
    body: formData,
  })
}

// ---- Pipeline ----

export function startPipeline(projectId: string): Promise<{ enqueued_jobs: Job[] }> {
  return request(`/projects/${projectId}/pipeline/start`, {
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
  return request(`/projects/${projectId}/sources/${sourceId}/reprocess${toQueryString({ confirm })}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ steps, params }),
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

export function getSegments(projectId: string, params: GetSegmentsParams): Promise<PaginatedSegments> {
  return request(`/projects/${projectId}/segments${toQueryString(params as Record<string, unknown>)}`)
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
): Promise<{ affected_count: number }> {
  return request(`/projects/${projectId}/segments/bulk`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
}

// ---- Export ----

export function triggerExport(projectId: string): Promise<unknown> {
  return request(`/projects/${projectId}/export`, {
    method: 'POST',
  })
}

export function getExportDownloadUrl(projectId: string): string {
  return `${BASE_URL}/projects/${projectId}/export/download`
}
