import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FailedJobsPanel } from './FailedJobsPanel'
import type { FailedJob } from '../../types/api'

const STORAGE_KEY = 'flipsync:dismissedFailedJobs'

function job(overrides: Partial<FailedJob>): FailedJob {
  return {
    id: 'job-1',
    type: 'vocal_separation',
    source_id: 'src-1',
    error: 'something broke',
    completed_at: null,
    ...overrides,
  }
}

describe('FailedJobsPanel retry gating', () => {
  it('shows Retry for retryable job types and calls onRetry with the job', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn()
    const failed = job({ type: 'vocal_separation' })
    render(<FailedJobsPanel failedJobs={[failed]} onRetry={onRetry} />)

    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetry).toHaveBeenCalledWith(failed)
  })

  it('shows Retry for a failed scout', () => {
    render(<FailedJobsPanel failedJobs={[job({ type: 'scout_speakers' })]} onRetry={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('hides Retry and shows re-upload guidance for extract_audio', () => {
    render(<FailedJobsPanel failedJobs={[job({ type: 'extract_audio' })]} onRetry={vi.fn()} />)

    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(screen.getByText(/remove this video and re-upload/i)).toBeInTheDocument()
    // Dismiss stays available.
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument()
  })

  it('hides Retry for transcription_segment (no segment id in the failed-job row)', () => {
    render(
      <FailedJobsPanel failedJobs={[job({ type: 'transcription_segment' })]} onRetry={vi.fn()} />,
    )
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument()
  })

  it('hides Retry for source-scoped jobs missing a source_id', () => {
    render(
      <FailedJobsPanel failedJobs={[job({ type: 'diarisation', source_id: null })]} onRetry={vi.fn()} />,
    )
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  it('gates each row independently', () => {
    render(
      <FailedJobsPanel
        failedJobs={[
          job({ id: 'a', type: 'extract_audio' }),
          job({ id: 'b', type: 'diarisation' }),
        ]}
        onRetry={vi.fn()}
      />,
    )
    const rows = screen.getAllByText(/failed$/i).map((el) => el.closest('div')!.parentElement!)
    expect(rows).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: 'Retry' })).toHaveLength(1)
    const diarisationRow = screen.getByText('Matching speaker failed').closest('div')!.parentElement!
    expect(within(diarisationRow).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })
})

describe('FailedJobsPanel dismissal persistence', () => {
  beforeEach(() => localStorage.clear())

  it('hides a job whose id is already stored as dismissed (survives reload)', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(['job-1']))
    const { container } = render(
      <FailedJobsPanel failedJobs={[job({ id: 'job-1' })]} onRetry={vi.fn()} />,
    )
    // Only-dismissed → panel renders nothing.
    expect(container).toBeEmptyDOMElement()
  })

  it('persists a dismissal to localStorage', async () => {
    const user = userEvent.setup()
    render(<FailedJobsPanel failedJobs={[job({ id: 'job-1' })]} onRetry={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual(['job-1'])
  })

  it('prunes stored ids no longer present in failedJobs', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(['old-job', 'job-1']))
    render(<FailedJobsPanel failedJobs={[job({ id: 'job-1' })]} onRetry={vi.fn()} />)
    // 'old-job' is gone from the API's list → pruned from storage; 'job-1' kept.
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual(['job-1'])
  })
})
