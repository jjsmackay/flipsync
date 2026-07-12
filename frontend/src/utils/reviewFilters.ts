// Work-queue detection for the review page's "all reviewed" completion state.

/** The statuses that make up the review work queue. */
export const WORK_QUEUE_STATUSES: ReadonlySet<string> = new Set(['pending', 'maybe'])

/**
 * True iff the status filter (a CSV of statuses) selects only work-queue
 * statuses — the full pending+maybe queue or either on its own. An 'All'
 * filter (every status) or any filter including reviewed statuses is NOT a
 * work queue, so an empty result there means "no match", not "all reviewed".
 */
export function isWorkQueueFilter(statusCsv: string): boolean {
  const selected = statusCsv
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  return selected.length > 0 && selected.every((s) => WORK_QUEUE_STATUSES.has(s))
}
