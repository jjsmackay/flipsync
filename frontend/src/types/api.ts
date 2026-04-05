// ---- Status enums ----

export type ProjectStatus = 'new' | 'processing' | 'review' | 'complete'

export type SegmentStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'maybe'
  | 'below_threshold'
  | 'clipping_warning'
  | 'auto_rejected'

export type SourceStatus =
  | 'uploading'
  | 'extracting'
  | 'step1_pending'
  | 'step1_running'
  | 'step1_failed'
  | 'step2_pending'
  | 'step2_running'
  | 'step2_failed'
  | 'complete'
  | 'extraction_failed'

// ---- Projects ----

export interface ProjectStats {
  approved_count: number
  approved_duration_secs: number
  pending_count: number
}

export interface ProjectSummary {
  id: string
  name: string
  status: ProjectStatus
  created_at: string
  updated_at: string
  stats: ProjectStats
}

export interface ProjectConfig {
  whisper_model: string
  language: string
  match_threshold: number
  target_duration_secs: number
}

export interface SourceCoverage {
  source_id: string
  filename: string
  status: SourceStatus
  coverage_ratio: number
  low_coverage_warning: boolean
  error: string | null
}

export interface ProjectDetailStats extends ProjectStats {
  total_segments: number
  maybe_count: number
  rejected_count: number
  below_threshold_count: number
  source_coverage: SourceCoverage[]
}

export interface JobSummary {
  id: string
  type: string
  status: string
  progress: number | null
}

export interface FailedJob {
  id: string
  type: string
  source_id: string | null
  error: string | null
  completed_at: string | null
}

export interface ProjectDetail extends ProjectSummary {
  config: ProjectConfig
  stats: ProjectDetailStats
  active_jobs: JobSummary[]
  recent_failed_jobs: FailedJob[]
}

// ---- Segments ----

export interface Segment {
  id: string
  source_id: string
  source_filename: string
  start_secs: number
  end_secs: number
  duration_secs: number
  match_confidence: number
  transcript: string | null
  transcript_edited: string | null
  transcript_confidence: number | null
  status: SegmentStatus
  clipping_warning: boolean
  audio_url: string
}

export interface Pagination {
  page: number
  per_page: number
  total: number
  pages: number
}

export interface PaginatedSegments {
  segments: Segment[]
  pagination: Pagination
}

// ---- Jobs ----

export interface Job {
  id: string
  type: string
  status: string
  progress: number | null
  source_id: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  error: string | null
}

// ---- Request bodies ----

export interface CreateProjectRequest {
  name: string
  whisper_model: string
  language: string
  match_threshold: number
  target_duration_secs: number
}

export interface PatchProjectRequest {
  name?: string
  match_threshold?: number
  target_duration_secs?: number
  whisper_model?: string
  language?: string
}

export interface PatchSegmentRequest {
  status?: SegmentStatus
  transcript_edited?: string | null
}

export interface BulkFilter {
  status?: SegmentStatus
  source_id?: string
  min_confidence?: number
  max_confidence?: number
  min_duration?: number
  max_duration?: number
}

export interface BulkSegmentRequest {
  action: 'approve' | 'reject' | 'maybe' | 'pending'
  filter: BulkFilter
}

export interface GetSegmentsParams {
  status?: SegmentStatus
  source_id?: string
  min_confidence?: number
  max_confidence?: number
  min_duration?: number
  max_duration?: number
  sort?: string
  order?: 'asc' | 'desc'
  page?: number
  per_page?: number
  count_only?: boolean
}
