import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ComparePanel } from './ComparePanel'
import { createPreview, deletePreview, getPreviews, getSegments } from '../../api/client'
import type { Model, Preview, Segment } from '../../types/api'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    createPreview: vi.fn(), deletePreview: vi.fn(), getPreviews: vi.fn(),
    getSegments: vi.fn(), getProject: vi.fn(),
  }
})

const model: Model = {
  id: 'model-1234567890', project_id: 'p1', status: 'ready', engine: 'xtts', dataset_mode: 'approved',
  min_confidence: null, segment_count: 10, dataset_duration_secs: 120,
  dataset_manifest_path: 'models/m/dataset.json', checkpoint_dir: 'models/m',
  params: null, eval_loss: null, error: null,
  created_at: '2026-07-14T00:00:00Z', updated_at: '2026-07-14T00:00:00Z',
}

const seg: Segment = {
  id: 'seg-1', source_id: 's1', source_filename: 'ep01.mkv',
  start_secs: 0, end_secs: 5, duration_secs: 5, match_confidence: 0.95,
  transcript: 'the quick brown fox', transcript_edited: null,
  transcript_confidence: 0.9, status: 'approved', clipping_warning: false,
  flags: [], audio_url: '/projects/p1/segments/seg-1/audio',
}

const paginated = { segments: [seg], pagination: { page: 1, per_page: 50, total: 1, pages: 1 } }

const SAMPLING = {
  temperature: 0.9, speed: 1.1, top_k: 30, top_p: 0.7, repetition_penalty: 8,
  enable_text_splitting: true,
}

function preview(over: Partial<Preview>): Preview {
  return {
    id: 'x', status: 'complete', text: 'line', model_id: null, conditioning: null,
    segment_id: 'seg-9', sampling: SAMPLING, created_at: '2026-07-13T00:00:00Z', ...over,
  }
}

beforeEach(() => {
  vi.mocked(createPreview).mockReset()
  vi.mocked(deletePreview).mockReset().mockResolvedValue(undefined)
  vi.mocked(getPreviews).mockReset().mockResolvedValue({ previews: [] })
  vi.mocked(getSegments).mockReset().mockResolvedValue(paginated)
})

describe('ComparePanel', () => {
  it('lists approved segments and generates a compare preview for the selected one', async () => {
    vi.mocked(createPreview).mockResolvedValue({ enqueued_job: { id: 'job-1', type: 'preview' } })
    render(<ComparePanel projectId="p1" models={[model]} />)

    // Picker fetches approved + auto_approved segments
    await waitFor(() => expect(getSegments).toHaveBeenCalled())
    const [, params] = vi.mocked(getSegments).mock.calls[0]
    expect(params).toMatchObject({ status: 'approved,auto_approved' })

    // Select the segment, then generate
    fireEvent.click(await screen.findByText(/quick brown fox/))
    fireEvent.click(screen.getByRole('button', { name: /generate/i }))

    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body).toMatchObject({ segment_id: 'seg-1', model_id: 'model-1234567890' })
    expect(body.text).toBeUndefined()
  })

  it('passes the search text as q', async () => {
    render(<ComparePanel projectId="p1" models={[model]} />)
    await waitFor(() => expect(getSegments).toHaveBeenCalled())

    fireEvent.change(screen.getByPlaceholderText(/search transcripts/i), {
      target: { value: 'fox' },
    })
    await waitFor(() => {
      const calls = vi.mocked(getSegments).mock.calls
      expect(calls[calls.length - 1][1]).toMatchObject({ q: 'fox' })
    })
  })

  it('shows Original and Clone players when the preview completes', async () => {
    vi.mocked(createPreview).mockResolvedValue({ enqueued_job: { id: 'job-1', type: 'preview' } })
    vi.mocked(getPreviews).mockResolvedValue({
      previews: [preview({ id: 'job-1', text: 'a different finished line',
                           model_id: 'model-1234567890', segment_id: 'seg-1' })],
    })
    // Clone audio is blob-fetched; stub fetch.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(new Blob(['x'], { type: 'audio/wav' }))))
    vi.stubGlobal('URL', Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:clone'), revokeObjectURL: vi.fn(),
    }))

    render(<ComparePanel projectId="p1" models={[model]} />)
    fireEvent.click(await screen.findByText(/quick brown fox/))
    fireEvent.click(screen.getByRole('button', { name: /generate/i }))

    await waitFor(() => expect(screen.getByText('Original')).toBeTruthy())
    await waitFor(() => expect(screen.getByText('Clone')).toBeTruthy())
    vi.unstubAllGlobals()
  })

  it('lists past compares (previews with a segment_id) as history', async () => {
    vi.mocked(getPreviews).mockResolvedValue({
      previews: [
        preview({ id: 'old-1', text: 'an old line', segment_id: 'seg-9' }),
        preview({ id: 'plain', text: 'not a compare', segment_id: null }),
      ],
    })
    render(<ComparePanel projectId="p1" models={[model]} />)

    await waitFor(() => expect(screen.getByText(/an old line/)).toBeTruthy())
    expect(screen.queryByText(/not a compare/)).toBeNull()
  })

  it('shows model + sampling provenance on a past comparison', async () => {
    vi.mocked(getPreviews).mockResolvedValue({
      previews: [preview({ id: 'old-1', text: 'an old line', model_id: 'model-1234567890' })],
    })
    render(<ComparePanel projectId="p1" models={[model]} advanced />)

    await waitFor(() => expect(screen.getByText(/an old line/)).toBeTruthy())
    // Reviewed model label + the sampling knobs (top-k/top-p shown under advanced).
    expect(screen.getByText(/Reviewed · model-12.*temp 0\.9.*speed 1\.1.*top-k 30.*top-p 0\.7/)).toBeTruthy()
  })

  it('deletes a past comparison and reloads the history', async () => {
    vi.mocked(getPreviews)
      .mockResolvedValueOnce({ previews: [preview({ id: 'old-1', text: 'doomed line' })] })
      .mockResolvedValue({ previews: [] })
    render(<ComparePanel projectId="p1" models={[model]} />)

    await waitFor(() => expect(screen.getByText(/doomed line/)).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /^delete$/i }))       // arm confirm
    fireEvent.click(screen.getByRole('button', { name: /^delete$/i }))       // confirm

    await waitFor(() => expect(deletePreview).toHaveBeenCalledWith('p1', 'old-1'))
    await waitFor(() => expect(screen.queryByText(/doomed line/)).toBeNull())
  })

  it('disables generate with no ready model', async () => {
    render(<ComparePanel projectId="p1" models={[]} />)
    fireEvent.click(await screen.findByText(/quick brown fox/))
    expect((screen.getByRole('button', { name: /generate/i }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('sends XTTS sampling values for an xtts model', async () => {
    vi.mocked(createPreview).mockResolvedValue({ enqueued_job: { id: 'job-1', type: 'preview' } })
    render(<ComparePanel projectId="p1" models={[model]} />)
    fireEvent.click(await screen.findByText(/quick brown fox/))
    fireEvent.click(screen.getByRole('button', { name: /generate/i }))
    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body).toMatchObject({ temperature: 0.65, speed: 1, top_k: 50, top_p: 0.85 })
  })

  it('omits every sampling knob and hides the sliders for a gpt_sovits model', async () => {
    vi.mocked(createPreview).mockResolvedValue({ enqueued_job: { id: 'job-1', type: 'preview' } })
    const gptModel: Model = { ...model, id: 'model-gpt', engine: 'gpt_sovits' }
    render(<ComparePanel projectId="p1" models={[gptModel]} />)

    expect(screen.queryByLabelText('Temperature')).toBeNull()
    expect(screen.queryByLabelText('Speed')).toBeNull()

    fireEvent.click(await screen.findByText(/quick brown fox/))
    fireEvent.click(screen.getByRole('button', { name: /generate/i }))
    await waitFor(() => expect(createPreview).toHaveBeenCalled())
    const [, body] = vi.mocked(createPreview).mock.calls[0]
    expect(body).toMatchObject({ segment_id: 'seg-1', model_id: 'model-gpt' })
    for (const key of ['temperature', 'speed', 'repetition_penalty', 'top_k', 'top_p',
                       'enable_text_splitting']) {
      expect(body).not.toHaveProperty(key)
    }
  })
})
