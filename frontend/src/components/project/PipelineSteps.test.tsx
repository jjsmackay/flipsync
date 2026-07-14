import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { PipelineSteps } from './PipelineSteps'
import { TUNING_DEFAULTS } from '../../utils/tuning'
import type { EngineInfo, ProjectDetail, SourceStatus, JobSummary } from '../../types/api'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return { ...actual, patchProject: vi.fn() }
})

function makeProject(overrides: {
  sourceStatuses?: SourceStatus[]
  activeJobs?: Partial<JobSummary>[]
  totalSegments?: number
}): ProjectDetail {
  const { sourceStatuses = ['complete'], activeJobs = [], totalSegments = 10 } = overrides
  return {
    id: 'p1',
    name: 'Test',
    status: 'review',
    created_at: '',
    updated_at: '',
    reference_path: '/data/ref.wav',
    reference_origin: null,
    config: {
      whisper_model: 'large-v2',
      language: null,
      match_threshold: 0.75,
      target_duration_secs: 1800,
      auto_approve_enabled: false,
      auto_approve_match_threshold: 0.9,
      auto_approve_transcript_threshold: 0.9,
      ...TUNING_DEFAULTS,
    },
    active_jobs: activeJobs.map((j, i) => ({
      id: j.id ?? `j${i}`,
      type: j.type ?? 'vocal_separation',
      status: j.status ?? 'running',
      progress: j.progress ?? null,
    })),
    recent_failed_jobs: [],
    stats: {
      approved_count: 0,
      approved_duration_secs: 0,
      pending_count: totalSegments,
      maybe_count: 0,
      total_segments: totalSegments,
      auto_approved_count: 0,
      rejected_count: 0,
      below_threshold_count: 0,
      source_coverage: sourceStatuses.map((s, i) => ({
        source_id: `s${i}`,
        filename: `file${i}.mp4`,
        status: s,
        coverage_ratio: 0,
        low_coverage_warning: false,
        error: null,
      })),
    },
  }
}

function renderSteps(
  project: ProjectDetail,
  handlers: Partial<{
    onReprocessAll: (steps: string[]) => void
    onRunTranscription: () => void
    onOpenCompare: () => void
  }> = {},
) {
  render(
    <MemoryRouter>
    <PipelineSteps
      project={project}
      voiceTrainingEnabled={false}
      onSaved={() => {}}
      onReprocessAll={handlers.onReprocessAll ?? (() => {})}
      onRunTranscription={handlers.onRunTranscription ?? (() => {})}
      onOpenCompare={handlers.onOpenCompare ?? (() => {})}
    />
    </MemoryRouter>,
  )
}

describe('PipelineSteps', () => {
  it('renders the five step rows with status chips', () => {
    renderSteps(makeProject({}))
    expect(screen.getByText('Separate vocals')).toBeInTheDocument()
    expect(screen.getByText('Match speaker')).toBeInTheDocument()
    expect(screen.getByText('Transcribe')).toBeInTheDocument()
    expect(screen.getByText('Review')).toBeInTheDocument()
    expect(screen.getByText('Clean & package')).toBeInTheDocument()
    // Complete project mid-review: the machine steps read Done, the Review
    // row counts the work owed.
    expect(screen.getAllByText('Done')).toHaveLength(3)
    expect(screen.getByText('10 to review')).toBeInTheDocument()
    expect(screen.getByText('Runs during export')).toBeInTheDocument()
  })

  it('shows segment count chips and duration in the Review row', () => {
    const p = makeProject({})
    p.stats.approved_count = 3
    p.stats.rejected_count = 1
    renderSteps(p)
    expect(screen.getByText('3 approved')).toBeInTheDocument()
    expect(screen.getByText('10 pending')).toBeInTheDocument()
    expect(screen.getByText('1 rejected')).toBeInTheDocument()
    expect(screen.getByText(/Approved duration/)).toBeInTheDocument()
  })

  it('holds the review thresholds in the Review row settings disclosure', () => {
    renderSteps(makeProject({}))
    // ProjectSettingsPanel's match-threshold slider renders inside the row.
    expect(screen.getByLabelText(/Match threshold/)).toBeInTheDocument()
  })

  it('puts the export flow on the Clean & package row', () => {
    renderSteps(makeProject({}))
    expect(screen.getByRole('button', { name: /Export dataset/ })).toBeInTheDocument()
  })

  it('links the Review row to the review queue', () => {
    renderSteps(makeProject({}))
    expect(screen.getByRole('link', { name: 'Open review →' })).toHaveAttribute(
      'href',
      '/projects/p1/review',
    )
  })

  it('marks the Review row Done once nothing is left to review', () => {
    const p = makeProject({ totalSegments: 0 })
    p.stats.pending_count = 0
    p.stats.maybe_count = 0
    p.stats.approved_count = 6
    p.stats.total_segments = 6
    renderSteps(p)
    expect(screen.getAllByText('Done')).toHaveLength(4)
  })

  it('shows Running on the active step', () => {
    renderSteps(
      makeProject({
        sourceStatuses: ['separation_running'],
        activeJobs: [{ type: 'vocal_separation' }],
      }),
    )
    expect(screen.getByText('Running')).toBeInTheDocument()
  })

  it('re-runs separation across sources', () => {
    const onReprocessAll = vi.fn()
    renderSteps(makeProject({}), { onReprocessAll })
    const rerunButtons = screen.getAllByRole('button', { name: 'Re-run' })
    fireEvent.click(rerunButtons[0])
    expect(onReprocessAll).toHaveBeenCalledWith(['separation', 'diarisation'])
    fireEvent.click(rerunButtons[1])
    expect(onReprocessAll).toHaveBeenCalledWith(['diarisation'])
  })

  it('disables re-run while a pipeline job is active', () => {
    renderSteps(
      makeProject({
        sourceStatuses: ['separation_running'],
        activeJobs: [{ type: 'vocal_separation' }],
      }),
    )
    for (const btn of screen.getAllByRole('button', { name: 'Re-run' })) {
      expect(btn).toBeDisabled()
    }
  })

  it('runs transcription and disables while a transcription job is active', () => {
    const onRunTranscription = vi.fn()
    renderSteps(makeProject({}), { onRunTranscription })
    // Steps complete → transcribe chip is Done → button reads Re-run; it's the
    // one on the Transcribe row (index 2 of the three Re-run buttons).
    fireEvent.click(screen.getAllByRole('button', { name: 'Re-run' })[2])
    expect(onRunTranscription).toHaveBeenCalled()
  })

  it('disables the transcribe button while transcription runs', () => {
    renderSteps(
      makeProject({ activeJobs: [{ type: 'transcription_bulk' }] }),
    )
    expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled()
  })

  it('opens the compare modal from the cleanup row', () => {
    const onOpenCompare = vi.fn()
    renderSteps(makeProject({}), { onOpenCompare })
    fireEvent.click(screen.getByRole('button', { name: 'Compare…' }))
    expect(onOpenCompare).toHaveBeenCalled()
  })

  it('disables Compare… when there are no segments', () => {
    renderSteps(makeProject({ sourceStatuses: ['separation_pending'], totalSegments: 0 }))
    expect(screen.getByRole('button', { name: 'Compare…' })).toBeDisabled()
  })

  it('hides the Train row when voice training is disabled', () => {
    renderSteps(makeProject({}))
    expect(screen.queryByText('Train')).not.toBeInTheDocument()
  })

  it('shows the Train row with a model-aware chip when voice training is enabled', () => {
    render(
      <MemoryRouter>
        <PipelineSteps
          project={makeProject({})}
          voiceTrainingEnabled={true}
          onSaved={() => {}}
          onReprocessAll={() => {}}
          onRunTranscription={() => {}}
          onOpenCompare={() => {}}
          models={[{ status: 'ready' } as never]}
          onGoToModels={() => {}}
        />
      </MemoryRouter>,
    )
    expect(screen.getByText('Train')).toBeInTheDocument()
    // ready model → Done (4th Done: three machine steps + train; review shows a count)
    expect(screen.getAllByText('Done')).toHaveLength(4)
    // The train affordance itself lives on the row (TrainPanel), with a link
    // out to the models/preview section.
    expect(screen.getByRole('button', { name: 'Train voice model' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Models →' })).toBeInTheDocument()
  })

  it('marks the Train row Running while a finetune job is active', () => {
    render(
      <MemoryRouter>
        <PipelineSteps
          project={makeProject({ activeJobs: [{ type: 'finetune' }] })}
          voiceTrainingEnabled={true}
          onSaved={() => {}}
          onReprocessAll={() => {}}
          onRunTranscription={() => {}}
          onOpenCompare={() => {}}
          models={[]}
          onGoToModels={() => {}}
        />
      </MemoryRouter>,
    )
    expect(screen.getByText('Running')).toBeInTheDocument()
  })

  const XTTS: EngineInfo = { id: 'xtts', name: 'XTTS-v2', healthy: true, languages: ['en'] }
  const XTTS_UNHEALTHY: EngineInfo = { ...XTTS, healthy: false }
  const GPT_SOVITS: EngineInfo = { id: 'gpt_sovits', name: 'GPT-SoVITS', healthy: true, languages: ['en'] }

  function renderTrainRow(engines?: EngineInfo[]) {
    render(
      <MemoryRouter>
        <PipelineSteps
          project={makeProject({})}
          voiceTrainingEnabled={true}
          engines={engines}
          onSaved={() => {}}
          onReprocessAll={() => {}}
          onRunTranscription={() => {}}
          onOpenCompare={() => {}}
          models={[]}
          onGoToModels={() => {}}
        />
      </MemoryRouter>,
    )
  }

  describe('Train row — legacy XTTS settings gating', () => {
    it('hides the persisted XTTS settings disclosure for a gpt-sovits-only deployment', () => {
      renderTrainRow([XTTS_UNHEALTHY, GPT_SOVITS])
      expect(screen.queryByLabelText('Epochs')).toBeNull()
    })

    it('keeps the persisted XTTS settings disclosure for an xtts-only deployment', () => {
      renderTrainRow([XTTS])
      expect(screen.getByLabelText('Epochs')).toBeInTheDocument()
    })

    it('keeps the persisted XTTS settings disclosure when both engines are healthy', () => {
      renderTrainRow([XTTS, GPT_SOVITS])
      expect(screen.getByLabelText('Epochs')).toBeInTheDocument()
    })

    it('defaults to showing it when `engines` is omitted (back-compat)', () => {
      renderTrainRow(undefined)
      expect(screen.getByLabelText('Epochs')).toBeInTheDocument()
    })
  })

  it('shows vocals players only for sources past separation', () => {
    renderSteps(
      makeProject({ sourceStatuses: ['complete', 'separation_pending'] }),
    )
    expect(screen.getByText('file0.mp4')).toBeInTheDocument()
    expect(screen.queryByText('file1.mp4')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '▶ vocals' })).toBeInTheDocument()
  })
})
