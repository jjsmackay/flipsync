import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BulkOperations, BULK_ACTION_SOURCES, effectiveBulkStatuses } from './BulkOperations'
import { ALL_SEGMENT_STATUSES } from '../../constants'
import { getSegmentsCount, bulkSegmentAction } from '../../api/client'

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    getSegmentsCount: vi.fn(),
    bulkSegmentAction: vi.fn(),
  }
})

const mockGetSegmentsCount = vi.mocked(getSegmentsCount)
const mockBulkSegmentAction = vi.mocked(bulkSegmentAction)

beforeEach(() => {
  vi.clearAllMocks()
  mockGetSegmentsCount.mockResolvedValue({ total: 7 })
  mockBulkSegmentAction.mockResolvedValue({ affected_count: 7 })
})

describe('effectiveBulkStatuses', () => {
  it('intersects a specific status with the action allowed set', () => {
    expect(effectiveBulkStatuses('approve', 'pending')).toEqual(['pending'])
    expect(effectiveBulkStatuses('approve', 'rejected')).toEqual([])
    expect(effectiveBulkStatuses('pending', 'rejected')).toEqual(['rejected'])
    expect(effectiveBulkStatuses('maybe', 'below_threshold')).toEqual([])
  })

  it('"Any" resolves to exactly the action allowed set', () => {
    for (const action of ['approve', 'reject', 'maybe', 'pending'] as const) {
      expect(effectiveBulkStatuses(action, '')).toEqual(
        ALL_SEGMENT_STATUSES.filter((s) => BULK_ACTION_SOURCES[action].includes(s)),
      )
      // "Any" never yields an empty intersection.
      expect(effectiveBulkStatuses(action, '').length).toBeGreaterThan(0)
    }
  })
})

async function renderExpanded() {
  const user = userEvent.setup()
  render(<BulkOperations projectId="proj-1" onApplied={vi.fn()} sources={[]} />)
  await user.click(screen.getByRole('button', { name: /Bulk operations/ }))
  return user
}

describe('BulkOperations preview intersection', () => {
  it('previews with the intersected status set, not the full status list', async () => {
    await renderExpanded()

    // Default action is approve, default status filter is Any.
    await waitFor(() => expect(mockGetSegmentsCount).toHaveBeenCalled())
    const [, filter] = mockGetSegmentsCount.mock.calls[0]
    expect(filter.status).toBe(BULK_ACTION_SOURCES.approve.join(','))
  })

  it('empty intersection: shows the hint, disables Apply, and skips the count call', async () => {
    const user = await renderExpanded()
    await waitFor(() => expect(mockGetSegmentsCount).toHaveBeenCalled())
    mockGetSegmentsCount.mockClear()

    const [actionSelect, statusSelect] = screen.getAllByRole('combobox')
    expect(actionSelect).toHaveValue('approve')
    await user.selectOptions(statusSelect, 'rejected')

    expect(await screen.findByText("Approve doesn't apply to rejected segments")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled()
    // No round-trip for a combination the server would reject wholesale.
    await new Promise((r) => setTimeout(r, 400)) // outlast the 300ms debounce
    expect(mockGetSegmentsCount).not.toHaveBeenCalled()
  })

  it('switching to a compatible action clears the hint and recounts', async () => {
    const user = await renderExpanded()
    const [actionSelect, statusSelect] = screen.getAllByRole('combobox')

    await user.selectOptions(statusSelect, 'rejected')
    expect(await screen.findByText("Approve doesn't apply to rejected segments")).toBeInTheDocument()

    mockGetSegmentsCount.mockClear()
    await user.selectOptions(actionSelect, 'pending') // pending ← rejected is allowed (undo wave)

    await waitFor(() => expect(mockGetSegmentsCount).toHaveBeenCalled())
    const [, filter] = mockGetSegmentsCount.mock.calls[0]
    expect(filter.status).toBe('rejected')
    expect(screen.queryByText(/doesn't apply to/)).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled())
  })

  it('Apply sends the same intersected filter the preview counted', async () => {
    const user = await renderExpanded()
    await waitFor(() =>
      expect(screen.getByText(/Affects 7 segments/)).toBeInTheDocument(),
    )

    await user.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(mockBulkSegmentAction).toHaveBeenCalled())
    const [, req] = mockBulkSegmentAction.mock.calls[0]
    expect(req.action).toBe('approve')
    expect(req.filter.status).toBe(BULK_ACTION_SOURCES.approve.join(','))
  })
})
