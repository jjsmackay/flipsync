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
  refetch: () => Promise<void>
}

export function useProjectPolling(
  projectId: string,
  options: UseProjectPollingOptions = {},
): UseProjectPollingResult {
  const { enabled = true } = options

  // Track whether there are active jobs. Start true so we fetch once on mount, then
  // stop polling when idle and resume (via refetch) on a user pipeline action.
  const [hasActiveJobs, setHasActiveJobs] = useState(true)

  const fetchFn = useCallback(() => getProject(projectId), [projectId])

  const handleData = useCallback((data: ProjectDetail) => {
    setHasActiveJobs(data.active_jobs.length > 0)
  }, [])

  // Poll every 3s while jobs are running; stop entirely when idle. refetch() still
  // works while stopped, and a refetch that reveals active jobs restarts polling.
  const { data: project, isLoading, error, refetch } = usePolling(fetchFn, {
    intervalMs: 3000,
    enabled: enabled && hasActiveJobs,
    onData: handleData,
  })

  return { project, isLoading, error, refetch }
}
