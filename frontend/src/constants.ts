import type { SegmentStatus } from './types/api'

// The complete set of segment statuses. Used where "All"/"Any" must be sent as an
// explicit list so a GET preview count matches a bulk POST's affected count (SC4),
// and so the timeline can fetch every segment regardless of the review filter.
export const ALL_SEGMENT_STATUSES: SegmentStatus[] = [
  'pending',
  'approved',
  'rejected',
  'maybe',
  'below_threshold',
  'clipping_warning',
  'auto_rejected',
  'auto_approved',
]

export const ALL_SEGMENT_STATUSES_CSV = ALL_SEGMENT_STATUSES.join(',')

// Statuses counted as approved (auto_approved is system-assigned but exported).
export const APPROVED_STATUSES: SegmentStatus[] = ['approved', 'auto_approved']

// Statuses the server actually exports (orchestrator export handler + manifest):
// the approved set plus segments a previous export flagged as clipping_warning.
// Confirm-panel counts must query this set or they undercount what Export ships.
export const EXPORTABLE_STATUSES: SegmentStatus[] = [...APPROVED_STATUSES, 'clipping_warning']
export const EXPORTABLE_STATUSES_CSV = EXPORTABLE_STATUSES.join(',')
