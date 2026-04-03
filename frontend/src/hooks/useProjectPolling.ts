import { useState, useCallback } from 'react'
import { getProject } from '../api/client'
import type { ProjectDetail } from '../types/api'
import { usePolling } from './usePolling'

interface UseProjectPollingOptions {
  enabled?: boolean
}

interface UseProjectPollingResult {
  project: ProjectDetail | null
  isLoading: boolean
  error: Error | null
}

export function useProjectPolling(
  projectId: string,
  options: UseProjectPollingOptions = {},
): UseProjectPollingResult {
  const { enabled = true } = options

  // Track whether there are active jobs so we can adjust the interval.
  // Start with true (short interval) on first load, then calm down once data arrives.
  const [hasActiveJobs, setHasActiveJobs] = useState(true)

  const fetchFn = useCallback(() => getProject(projectId), [projectId])

  const handleData = useCallback((data: ProjectDetail) => {
    setHasActiveJobs(data.active_jobs.length > 0)
  }, [])

  // Use 3s when jobs are running, 10s when idle.
  const intervalMs = hasActiveJobs ? 3000 : 10000

  const { data: project, isLoading, error } = usePolling(fetchFn, {
    intervalMs,
    enabled,
    onData: handleData,
  })

  return { project, isLoading, error }
}
