import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import type { GetSegmentsParams } from '../types/api'

export interface FilterState {
  status: string
  source_id: string
  min_confidence: number
  min_duration: number
  sort: string
  order: 'asc' | 'desc'
  page: number
}

export const DEFAULT_FILTER: FilterState = {
  status: 'pending,maybe',
  source_id: '',
  min_confidence: 0.75,
  min_duration: 0,
  sort: 'match_confidence',
  order: 'desc',
  page: 1,
}

export function useFilterState() {
  const [params, setParams] = useSearchParams()

  const filter: FilterState = {
    status: params.get('status') ?? DEFAULT_FILTER.status,
    source_id: params.get('source_id') ?? '',
    min_confidence: parseFloat(params.get('min_confidence') ?? '') || DEFAULT_FILTER.min_confidence,
    min_duration: parseFloat(params.get('min_duration') ?? '') || 0,
    sort: params.get('sort') ?? DEFAULT_FILTER.sort,
    order: (params.get('order') ?? DEFAULT_FILTER.order) as 'asc' | 'desc',
    page: parseInt(params.get('page') ?? '1', 10) || 1,
  }

  const setFilter = useCallback(
    (update: Partial<FilterState>) => {
      setParams(prev => {
        const next = new URLSearchParams(prev)
        for (const [k, v] of Object.entries(update)) {
          if (v === '' || v === null || v === undefined || v === 0) {
            next.delete(k)
          } else {
            next.set(k, String(v))
          }
        }
        if (!('page' in update)) next.set('page', '1')
        return next
      })
    },
    [setParams],
  )

  function toApiParams(overrides?: Partial<GetSegmentsParams>): GetSegmentsParams {
    const p: Record<string, unknown> = {
      page: filter.page,
      per_page: 50,
      sort: filter.sort,
      order: filter.order,
    }
    if (filter.status) p.status = filter.status
    if (filter.source_id) p.source_id = filter.source_id
    if (filter.min_confidence > 0) p.min_confidence = filter.min_confidence
    if (filter.min_duration > 0) p.min_duration = filter.min_duration
    return { ...p, ...overrides } as GetSegmentsParams
  }

  return { filter, setFilter, toApiParams }
}
