import { describe, it, expect } from 'vitest'
import { isWorkQueueFilter } from './reviewFilters'
import { ALL_SEGMENT_STATUSES_CSV } from '../constants'

describe('isWorkQueueFilter', () => {
  it('is true for the default work queue (pending,maybe)', () => {
    expect(isWorkQueueFilter('pending,maybe')).toBe(true)
    expect(isWorkQueueFilter('maybe,pending')).toBe(true)
  })

  it('is true for pending or maybe alone (e.g. the "View the Maybe pile" flow)', () => {
    expect(isWorkQueueFilter('pending')).toBe(true)
    expect(isWorkQueueFilter('maybe')).toBe(true)
  })

  it('is false for the "All" filter — substring matching on the CSV was the bug', () => {
    expect(isWorkQueueFilter(ALL_SEGMENT_STATUSES_CSV)).toBe(false)
  })

  it('is false when reviewed statuses are mixed in', () => {
    expect(isWorkQueueFilter('pending,approved')).toBe(false)
    expect(isWorkQueueFilter('approved')).toBe(false)
    expect(isWorkQueueFilter('rejected,maybe')).toBe(false)
  })

  it('is false for other unreviewed statuses like below_threshold', () => {
    expect(isWorkQueueFilter('below_threshold')).toBe(false)
  })

  it('is false for an empty filter', () => {
    expect(isWorkQueueFilter('')).toBe(false)
  })

  it('tolerates whitespace around entries', () => {
    expect(isWorkQueueFilter(' pending , maybe ')).toBe(true)
  })
})
