// ---- Status enums ----

export type ProjectStatus =
  | 'new'
  | 'ready'
  | 'processing'
  | 'awaiting_reference'
  | 'review'
  | 'exporting'
  | 'exported'

export type SegmentStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'maybe'
  | 'below_threshold'
  | 'clipping_warning'
  | 'auto_rejected'
  | 'auto_approved'

export type SourceStatus =
  | 'uploaded'
  | 'extracting'
  | 'extraction_failed'
  | 'separation_pending'
  | 'separation_running'
  | 'separation_failed'
  | 'diarisation_pending'
  | 'diarisation_running'
  | 'diarisation_failed'
  | 'complete'

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
  target_duration_secs?: number
}

export interface ProjectConfig {
  whisper_model: string
  language: string | null
  match_threshold: number
  target_duration_secs: number
  auto_approve_enabled: boolean
  auto_approve_match_threshold: number
  auto_approve_transcript_threshold: number
  whisper_batch_size: number
  whisper_compute_type: string
}

export const WHISPER_COMPUTE_TYPES = ['default', 'float16', 'int8_float16', 'int8'] as const

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
  auto_approved_count: number
  maybe_count: number
  rejected_count: number
  below_threshold_count: number
  source_coverage: SourceCoverage[]
}

export interface TrainingProgress {
  phase: string
  epoch: number
  total_epochs: number
  step: number
  total_steps: number
  train_loss: number | null
  eval_loss: number | null
  eta_secs: number | null
}

export interface JobSummary {
  id: string
  type: string
  status: string
  progress: number | null
  progress_detail?: TrainingProgress | null
}

export interface FailedJob {
  id: string
  type: string
  source_id: string | null
  error: string | null
  completed_at: string | null
}

// ---- Reference ----

export type ReferenceOrigin =
  | { type: 'uploaded' }
  | { type: 'diarise_pick'; source_id: string; speaker_label: string }

export interface PoolTurn {
  index: number
  start: number
  end: number
  duration: number
  sample_url: string
}

export interface SpeakerCandidate {
  speaker_label: string
  total_secs: number
  segment_count: number
  // Bounded pool of individual turn slices for curation. The reference is built
  // from these (longest-first up to 30 s) minus any the user excludes.
  pool: PoolTurn[]
}

export type ScoutStatus =
  | { status: 'running'; progress: number; source_id: string; speakers: SpeakerCandidate[] }
  | { status: 'failed'; source_id: string; error: string; speakers: SpeakerCandidate[] }
  | { status: 'complete'; source_id: string; speakers: SpeakerCandidate[] }

export interface ProjectDetail extends ProjectSummary {
  config: ProjectConfig
  stats: ProjectDetailStats
  active_jobs: JobSummary[]
  recent_failed_jobs: FailedJob[]
  reference_path: string | null
  reference_origin: ReferenceOrigin | null
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
  // Cluster-level secondary score from diarisation; null for segments cut
  // before migration 006, absent from older orchestrators.
  speaker_match_confidence?: number | null
  transcript: string | null
  transcript_edited: string | null
  transcript_confidence: number | null
  status: SegmentStatus
  clipping_warning: boolean
  flags: string[] | null
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
  language: string | null
  match_threshold: number
  target_duration_secs: number
}

export interface PatchProjectRequest {
  name?: string
  match_threshold?: number
  target_duration_secs?: number
  whisper_model?: string
  language?: string | null
  auto_approve_enabled?: boolean
  auto_approve_match_threshold?: number
  auto_approve_transcript_threshold?: number
  whisper_batch_size?: number
  whisper_compute_type?: string
}

export interface PatchSegmentRequest {
  status?: SegmentStatus
  transcript_edited?: string | null
}

export interface BulkFilter {
  // May be a single status or a comma-separated list (e.g. the full status set for "Any").
  status?: string
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

// ---- Models (v1.5) ----

export type ModelStatus = 'pending' | 'training' | 'ready' | 'failed' | 'cancelled'

export interface ModelParams {
  epochs: number
  batch_size: number
  grad_accum: number
  learning_rate: number
}

export interface Model {
  id: string
  project_id: string
  status: ModelStatus
  dataset_mode: 'approved' | 'auto'
  min_confidence: number | null
  segment_count: number | null
  dataset_duration_secs: number | null
  dataset_manifest_path: string | null
  checkpoint_dir: string | null
  params: ModelParams | null
  eval_loss: number | null
  error: string | null
  created_at: string
  updated_at: string
}

export interface CreateModelRequest {
  dataset?: {
    mode: 'approved' | 'auto'
    min_confidence?: number | null
  }
  params?: Partial<ModelParams>
}

// ---- Previews (v1.5) ----

export interface PreviewConditioning {
  source?: 'reference_clip' | 'segments_raw' | 'segments_cleaned'
  segment_count?: number
}

export interface Preview {
  id: string
  status: string
  text: string
  model_id: string | null
  conditioning: PreviewConditioning | null
  created_at: string
}

export interface CreatePreviewRequest {
  text: string
  model_id: string | null
  conditioning?: PreviewConditioning
}

export interface GetSegmentsParams {
  // May be a single status or a comma-separated list.
  status?: string
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
