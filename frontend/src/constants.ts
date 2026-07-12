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
]

export const ALL_SEGMENT_STATUSES_CSV = ALL_SEGMENT_STATUSES.join(',')
