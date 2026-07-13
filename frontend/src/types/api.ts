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
  // Pipeline tuning knobs (migration 011; align_words is migration 012).
  // Changing one applies on the next run of that stage — no retro-apply.
  // Bounds live in utils/tuning.ts and mirror the server-side validators.
  demucs_model: string
  demucs_shifts: number
  diar_min_speakers: number
  diar_max_speakers: number
  diar_min_segment_duration: number
  whisper_beam_size: number
  whisper_vad_filter: boolean
  align_words: boolean
  target_lufs: number
  highpass_hz: number
  silence_threshold_db: number
  silence_min_duration_secs: number
  xtts_epochs: number
  xtts_batch_size: number
  xtts_grad_accum: number
  xtts_learning_rate: number
}

export const WHISPER_COMPUTE_TYPES = ['default', 'float16', 'int8_float16', 'int8'] as const
export const DEMUCS_MODELS = ['htdemucs', 'htdemucs_ft', 'mdx_extra', 'bs_roformer'] as const

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

// The pipeline tuning knobs, as an optional subset — accepted on both project
// creation and PATCH (same server-side bounds; out-of-range → 422).
export interface TuningPatch {
  demucs_model?: string
  demucs_shifts?: number
  diar_min_speakers?: number
  diar_max_speakers?: number
  diar_min_segment_duration?: number
  whisper_beam_size?: number
  whisper_vad_filter?: boolean
  align_words?: boolean
  whisper_batch_size?: number
  whisper_compute_type?: string
  target_lufs?: number
  highpass_hz?: number
  silence_threshold_db?: number
  silence_min_duration_secs?: number
  xtts_epochs?: number
  xtts_batch_size?: number
  xtts_grad_accum?: number
  xtts_learning_rate?: number
}

export interface CreateProjectRequest extends TuningPatch {
  name: string
  whisper_model: string
  language: string | null
  match_threshold: number
  target_duration_secs: number
}

export interface PatchProjectRequest extends TuningPatch {
  name?: string
  match_threshold?: number
  target_duration_secs?: number
  whisper_model?: string
  language?: string | null
  auto_approve_enabled?: boolean
  auto_approve_match_threshold?: number
  auto_approve_transcript_threshold?: number
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
  // XTTS sampling knobs. Per-run only — not project config. Bounds mirror the
  // orchestrator's validators; defaults are coqui's except temperature.
  /** >0–2, default 0.65 */
  temperature?: number
  /** 0.25–2, default 1 — playback-rate multiplier */
  speed?: number
  /** 1–20, default 10 — raise to kill stutters/looping artefacts */
  repetition_penalty?: number
  /** 1–100, default 50 */
  top_k?: number
  /** >0–1, default 0.85 */
  top_p?: number
}

// ---- Tuning previews (ephemeral stage A/B) ----

export interface CleanupTuningParams {
  target_lufs: number
  highpass_hz: number
  silence_threshold_db: number
  silence_min_duration_secs: number
}

export interface CreateTuningPreviewRequest {
  // Stage-generic: 'cleanup' is the only stage this wave; 'separation' is the
  // planned follow-on.
  stage: 'cleanup'
  params: CleanupTuningParams
  target: { segment_id: string }
}

export interface TuningPreviewStatus {
  id: string
  status: string
  error: string | null
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
