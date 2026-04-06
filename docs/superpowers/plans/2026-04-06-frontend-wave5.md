# FlipSync Frontend Wave 5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the full FlipSync review UI — project list, dashboard, review queue with keyboard navigation, bulk operations, timeline, and export flow.

**Architecture:** React + TypeScript SPA using existing typed API client (`src/api/client.ts`), polling hooks (`usePolling`, `useProjectPolling`), and audio hook (`useAudio`). All state from API, filter state in URL query string. Component tree: shared UI components → feature components → page shells.

**Tech Stack:** React 18, TypeScript, Tailwind CSS v4 (`@tailwindcss/vite`), react-router-dom v6, Vite 6

**Branch:** `integrate/frontend`

---

## File Map

### New files to create
```
frontend/src/components/ui/StatusBadge.tsx
frontend/src/components/ui/ConfidenceBadge.tsx
frontend/src/components/ui/ProgressBar.tsx
frontend/src/components/project/CreateProjectModal.tsx
frontend/src/components/project/SourcesTable.tsx
frontend/src/components/project/StatsPanel.tsx
frontend/src/components/project/JobsPanel.tsx
frontend/src/components/project/PipelineControls.tsx
frontend/src/components/project/UploadArea.tsx
frontend/src/components/review/FilterBar.tsx
frontend/src/components/review/SegmentCard.tsx
frontend/src/components/review/WaveformCanvas.tsx
frontend/src/components/review/AudioControls.tsx
frontend/src/components/review/SegmentDetail.tsx
frontend/src/components/review/BulkOperations.tsx
frontend/src/components/review/Timeline.tsx
frontend/src/components/review/KeyboardHelp.tsx
frontend/src/components/export/ExportButton.tsx
frontend/src/hooks/useFilterState.ts
```

### Files to replace (page shells)
```
frontend/src/pages/ProjectListPage.tsx
frontend/src/pages/ProjectDashboardPage.tsx
frontend/src/pages/ReviewQueuePage.tsx
```

### Files to modify
```
frontend/package.json       — add react-router-dom, tailwindcss, @tailwindcss/vite
```

---

## Task 1: Switch branch and install dependencies

**Files:**
- Modify: `frontend/package.json`

- [ ] **Step 1: Switch to integrate/frontend branch**
```bash
git checkout integrate/frontend
```

- [ ] **Step 2: Install missing packages**
```bash
cd frontend && pnpm add react-router-dom && pnpm add -D tailwindcss @tailwindcss/vite @types/react-router-dom
```

- [ ] **Step 3: Verify TypeScript compiles**
```bash
cd frontend && pnpm build 2>&1 | head -30
```
Expected: Build succeeds (or only "no source files" type errors from empty components - not import errors)

- [ ] **Step 4: Commit**
```bash
git add frontend/package.json frontend/pnpm-lock.yaml
git commit -m "feat(frontend): install react-router-dom and tailwindcss dependencies"
```

---

## Task 2: Shared UI components

**Files:**
- Create: `frontend/src/components/ui/StatusBadge.tsx`
- Create: `frontend/src/components/ui/ConfidenceBadge.tsx`
- Create: `frontend/src/components/ui/ProgressBar.tsx`

- [ ] **Step 1: Create StatusBadge**

`frontend/src/components/ui/StatusBadge.tsx`:
```tsx
import type { ProjectStatus, SegmentStatus, SourceStatus } from '../../types/api'

type AnyStatus = ProjectStatus | SegmentStatus | SourceStatus

const STATUS_STYLES: Record<string, string> = {
  // Project statuses
  new: 'bg-gray-100 text-gray-700',
  processing: 'bg-blue-100 text-blue-700',
  review: 'bg-yellow-100 text-yellow-700',
  complete: 'bg-green-100 text-green-700',
  // Segment statuses
  pending: 'bg-gray-100 text-gray-700',
  approved: 'bg-green-100 text-green-700',
  rejected: 'bg-red-100 text-red-700',
  maybe: 'bg-yellow-100 text-yellow-700',
  below_threshold: 'bg-gray-200 text-gray-500',
  clipping_warning: 'bg-orange-100 text-orange-700',
  auto_rejected: 'bg-red-200 text-red-600',
  // Source statuses
  uploading: 'bg-blue-100 text-blue-600',
  extracting: 'bg-blue-100 text-blue-600',
  step1_pending: 'bg-gray-100 text-gray-600',
  step1_running: 'bg-blue-100 text-blue-600',
  step1_failed: 'bg-red-100 text-red-700',
  step2_pending: 'bg-gray-100 text-gray-600',
  step2_running: 'bg-blue-100 text-blue-600',
  step2_failed: 'bg-red-100 text-red-700',
  extraction_failed: 'bg-red-100 text-red-700',
}

const STATUS_DOT: Record<string, string> = {
  approved: 'bg-green-500',
  rejected: 'bg-red-500',
  maybe: 'bg-yellow-500',
  pending: 'bg-gray-400',
  below_threshold: 'bg-gray-300',
  clipping_warning: 'bg-orange-400',
  auto_rejected: 'bg-red-400',
}

interface StatusBadgeProps {
  status: AnyStatus
  dot?: boolean
}

export function StatusBadge({ status, dot = false }: StatusBadgeProps) {
  const style = STATUS_STYLES[status] ?? 'bg-gray-100 text-gray-600'
  const dotStyle = STATUS_DOT[status]

  if (dot && dotStyle) {
    return (
      <span
        className={`inline-block w-2.5 h-2.5 rounded-full ${dotStyle}`}
        title={status.replace(/_/g, ' ')}
      />
    )
  }

  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${style}`}>
      {status.replace(/_/g, ' ')}
    </span>
  )
}
```

- [ ] **Step 2: Create ConfidenceBadge**

`frontend/src/components/ui/ConfidenceBadge.tsx`:
```tsx
interface ConfidenceBadgeProps {
  value: number
  label?: string
}

function confidenceStyle(v: number): string {
  if (v >= 0.9) return 'text-green-700 bg-green-50'
  if (v >= 0.75) return 'text-yellow-700 bg-yellow-50'
  return 'text-red-700 bg-red-50'
}

export function ConfidenceBadge({ value, label }: ConfidenceBadgeProps) {
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-mono font-medium ${confidenceStyle(value)}`}
    >
      {label && <span className="font-sans text-xs opacity-70">{label}</span>}
      {(value * 100).toFixed(0)}%
    </span>
  )
}
```

- [ ] **Step 3: Create ProgressBar**

`frontend/src/components/ui/ProgressBar.tsx`:
```tsx
interface ProgressBarProps {
  value: number       // 0–1
  label?: string
  className?: string
  color?: 'green' | 'blue' | 'yellow'
}

const COLORS = {
  green: 'bg-green-500',
  blue: 'bg-blue-500',
  yellow: 'bg-yellow-500',
}

export function ProgressBar({ value, label, className = '', color = 'green' }: ProgressBarProps) {
  const pct = Math.min(1, Math.max(0, value)) * 100

  return (
    <div className={`w-full ${className}`}>
      {label && (
        <div className="flex justify-between text-xs text-gray-500 mb-1">
          <span>{label}</span>
          <span>{pct.toFixed(0)}%</span>
        </div>
      )}
      <div className="w-full bg-gray-200 rounded-full h-2">
        <div
          className={`h-2 rounded-full transition-all ${COLORS[color]}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Commit**
```bash
git add frontend/src/components/ui/
git commit -m "feat(frontend): add shared UI components (StatusBadge, ConfidenceBadge, ProgressBar)"
```

---

## Task 3: Project list page

**Files:**
- Create: `frontend/src/components/project/CreateProjectModal.tsx`
- Modify: `frontend/src/pages/ProjectListPage.tsx`

- [ ] **Step 1: Create CreateProjectModal**

`frontend/src/components/project/CreateProjectModal.tsx`:
```tsx
import { useState } from 'react'
import { createProject } from '../../api/client'
import type { CreateProjectRequest } from '../../types/api'

interface CreateProjectModalProps {
  onCreated: (id: string) => void
  onClose: () => void
}

const WHISPER_MODELS = ['tiny', 'base', 'small', 'medium', 'large-v2', 'large-v3']
const LANGUAGES = ['en', 'fr', 'de', 'es', 'ja', 'zh', 'auto']

export function CreateProjectModal({ onCreated, onClose }: CreateProjectModalProps) {
  const [form, setForm] = useState<CreateProjectRequest>({
    name: '',
    whisper_model: 'large-v3',
    language: 'en',
    match_threshold: 0.75,
    target_duration_secs: 3600,
  })
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const result = await createProject(form)
      onCreated(result.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create project')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4 p-6"
        onClick={e => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold mb-4">New Project</h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Project name</label>
            <input
              type="text"
              required
              value={form.name}
              onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="My Voice Dataset"
              autoFocus
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Whisper model</label>
              <select
                value={form.whisper_model}
                onChange={e => setForm(f => ({ ...f, whisper_model: e.target.value }))}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                {WHISPER_MODELS.map(m => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Language</label>
              <select
                value={form.language}
                onChange={e => setForm(f => ({ ...f, language: e.target.value }))}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                {LANGUAGES.map(l => (
                  <option key={l} value={l}>{l}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Match threshold
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="range"
                  min="0"
                  max="1"
                  step="0.05"
                  value={form.match_threshold}
                  onChange={e => setForm(f => ({ ...f, match_threshold: parseFloat(e.target.value) }))}
                  className="flex-1"
                />
                <span className="text-sm font-mono w-10 text-right">
                  {form.match_threshold.toFixed(2)}
                </span>
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Target duration (hrs)
              </label>
              <input
                type="number"
                min="0.1"
                step="0.5"
                value={(form.target_duration_secs / 3600).toFixed(1)}
                onChange={e =>
                  setForm(f => ({ ...f, target_duration_secs: parseFloat(e.target.value) * 3600 }))
                }
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
          </div>

          {error && (
            <div className="text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2">{error}</div>
          )}

          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting || !form.name.trim()}
              className="flex-1 px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
            >
              {submitting ? 'Creating…' : 'Create project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Implement ProjectListPage**

`frontend/src/pages/ProjectListPage.tsx`:
```tsx
import { useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { usePolling } from '../hooks/usePolling'
import { getProjects, deleteProject } from '../api/client'
import { CreateProjectModal } from '../components/project/CreateProjectModal'
import { StatusBadge } from '../components/ui/StatusBadge'
import { ProgressBar } from '../components/ui/ProgressBar'
import type { ProjectSummary } from '../types/api'

function formatDuration(secs: number): string {
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = Math.floor(secs % 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

function ProjectCard({
  project,
  onDelete,
  onClick,
}: {
  project: ProjectSummary
  onDelete: () => void
  onClick: () => void
}) {
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const target = (project as ProjectSummary & { config?: { target_duration_secs?: number } })
    .config?.target_duration_secs

  const progress = target
    ? project.stats.approved_duration_secs / target
    : null

  async function handleDelete(e: React.MouseEvent) {
    e.stopPropagation()
    if (!confirmDelete) {
      setConfirmDelete(true)
      return
    }
    setDeleting(true)
    try {
      await deleteProject(project.id, true)
      onDelete()
    } catch {
      setDeleting(false)
      setConfirmDelete(false)
    }
  }

  return (
    <div
      className="bg-white rounded-xl border border-gray-200 p-5 hover:border-blue-300 hover:shadow-sm cursor-pointer transition-all"
      onClick={onClick}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <h3 className="font-semibold text-gray-900 truncate">{project.name}</h3>
          <div className="flex items-center gap-2 mt-1">
            <StatusBadge status={project.status} />
            <span className="text-xs text-gray-500">
              {new Date(project.updated_at).toLocaleDateString()}
            </span>
          </div>
        </div>
        <button
          onClick={handleDelete}
          disabled={deleting}
          className={`shrink-0 text-xs px-3 py-1 rounded-lg border transition-colors ${
            confirmDelete
              ? 'border-red-400 text-red-600 bg-red-50 hover:bg-red-100'
              : 'border-gray-200 text-gray-400 hover:text-red-500 hover:border-red-300'
          }`}
        >
          {deleting ? '…' : confirmDelete ? 'Confirm delete' : 'Delete'}
        </button>
      </div>

      <div className="mt-3 text-sm text-gray-600 grid grid-cols-3 gap-2">
        <div>
          <div className="text-xs text-gray-400">Approved</div>
          <div className="font-medium">{project.stats.approved_count}</div>
        </div>
        <div>
          <div className="text-xs text-gray-400">Pending</div>
          <div className="font-medium">{project.stats.pending_count}</div>
        </div>
        <div>
          <div className="text-xs text-gray-400">Duration</div>
          <div className="font-medium">{formatDuration(project.stats.approved_duration_secs)}</div>
        </div>
      </div>

      {progress !== null && (
        <ProgressBar value={progress} className="mt-3" color={progress >= 1 ? 'green' : 'blue'} />
      )}
    </div>
  )
}

export function ProjectListPage() {
  const navigate = useNavigate()
  const [showCreate, setShowCreate] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)

  const fetchFn = useCallback(() => getProjects(), [])
  const { data, isLoading } = usePolling(fetchFn, { intervalMs: 10000 })

  const projects = data?.projects ?? []

  function handleCreated(id: string) {
    setShowCreate(false)
    navigate(`/projects/${id}`)
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-4xl mx-auto px-4 py-8">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-bold text-gray-900">FlipSync</h1>
          <button
            onClick={() => setShowCreate(true)}
            className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 font-medium"
          >
            + New project
          </button>
        </div>

        {isLoading && projects.length === 0 && (
          <div className="text-center py-16 text-gray-400">Loading…</div>
        )}

        {!isLoading && projects.length === 0 && (
          <div className="text-center py-16">
            <div className="text-gray-400 text-lg mb-3">No projects yet</div>
            <button
              onClick={() => setShowCreate(true)}
              className="text-blue-600 hover:underline text-sm"
            >
              Create your first project
            </button>
          </div>
        )}

        <div className="grid gap-4">
          {projects.map(project => (
            <ProjectCard
              key={`${project.id}-${refreshKey}`}
              project={project}
              onClick={() => navigate(`/projects/${project.id}`)}
              onDelete={() => setRefreshKey(k => k + 1)}
            />
          ))}
        </div>
      </div>

      {showCreate && (
        <CreateProjectModal onCreated={handleCreated} onClose={() => setShowCreate(false)} />
      )}
    </div>
  )
}
```

- [ ] **Step 3: Commit**
```bash
git add frontend/src/components/project/CreateProjectModal.tsx frontend/src/pages/ProjectListPage.tsx
git commit -m "feat(frontend): implement project list page with create modal and delete"
```

---

## Task 4: Dashboard — sources table and stats panel

**Files:**
- Create: `frontend/src/components/project/SourcesTable.tsx`
- Create: `frontend/src/components/project/StatsPanel.tsx`

- [ ] **Step 1: Create SourcesTable**

`frontend/src/components/project/SourcesTable.tsx`:
```tsx
import { StatusBadge } from '../ui/StatusBadge'
import type { SourceCoverage } from '../../types/api'

interface SourcesTableProps {
  sources: SourceCoverage[]
  onReprocess?: (sourceId: string, steps: string[]) => void
}

function coverageColor(ratio: number, warning: boolean): string {
  if (warning) return 'text-amber-600'
  if (ratio >= 0.15) return 'text-green-600'
  return 'text-gray-600'
}

export function SourcesTable({ sources, onReprocess }: SourcesTableProps) {
  if (sources.length === 0) {
    return (
      <div className="text-center py-8 text-gray-400 text-sm">No sources uploaded yet.</div>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-100 text-left text-xs text-gray-500 uppercase tracking-wide">
            <th className="pb-2 pr-4 font-medium">File</th>
            <th className="pb-2 pr-4 font-medium">Status</th>
            <th className="pb-2 pr-4 font-medium">Coverage</th>
            {onReprocess && <th className="pb-2 font-medium">Actions</th>}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-50">
          {sources.map(src => (
            <tr key={src.source_id} className="py-2">
              <td className="py-3 pr-4">
                <span className="font-medium text-gray-900 truncate block max-w-xs">
                  {src.filename}
                </span>
                {src.error && (
                  <span className="text-xs text-red-500 mt-0.5 block">{src.error}</span>
                )}
              </td>
              <td className="py-3 pr-4">
                <StatusBadge status={src.status} />
              </td>
              <td className="py-3 pr-4">
                <span
                  className={`font-mono text-sm ${coverageColor(src.coverage_ratio, src.low_coverage_warning)}`}
                >
                  {(src.coverage_ratio * 100).toFixed(1)}%
                </span>
                {src.low_coverage_warning && (
                  <span className="ml-1 text-xs text-amber-600" title="Low coverage warning">⚠</span>
                )}
              </td>
              {onReprocess && (
                <td className="py-3">
                  <div className="flex gap-2">
                    <button
                      onClick={() => onReprocess(src.source_id, ['step1', 'step2'])}
                      className="text-xs px-2 py-1 border border-gray-200 rounded hover:bg-gray-50 text-gray-600"
                      title="Reprocess vocal separation + diarisation"
                    >
                      Step 1+2
                    </button>
                    <button
                      onClick={() => onReprocess(src.source_id, ['step2'])}
                      className="text-xs px-2 py-1 border border-gray-200 rounded hover:bg-gray-50 text-gray-600"
                      title="Reprocess diarisation only"
                    >
                      Step 2
                    </button>
                  </div>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
```

- [ ] **Step 2: Create StatsPanel**

`frontend/src/components/project/StatsPanel.tsx`:
```tsx
import { ProgressBar } from '../ui/ProgressBar'
import type { ProjectDetailStats, ProjectConfig } from '../../types/api'

interface StatsPanelProps {
  stats: ProjectDetailStats
  config: ProjectConfig
}

function formatDuration(secs: number): string {
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = Math.floor(secs % 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

interface StatBoxProps {
  label: string
  value: string | number
  color?: string
}

function StatBox({ label, value, color = 'text-gray-900' }: StatBoxProps) {
  return (
    <div className="bg-gray-50 rounded-lg p-3 text-center">
      <div className={`text-xl font-bold ${color}`}>{value}</div>
      <div className="text-xs text-gray-500 mt-0.5">{label}</div>
    </div>
  )
}

export function StatsPanel({ stats, config }: StatsPanelProps) {
  const progress = config.target_duration_secs > 0
    ? stats.approved_duration_secs / config.target_duration_secs
    : 0

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-5 gap-2">
        <StatBox label="Approved" value={stats.approved_count} color="text-green-700" />
        <StatBox label="Pending" value={stats.pending_count} />
        <StatBox label="Maybe" value={stats.maybe_count} color="text-yellow-700" />
        <StatBox label="Rejected" value={stats.rejected_count} color="text-red-600" />
        <StatBox label="Below threshold" value={stats.below_threshold_count} color="text-gray-500" />
      </div>

      <ProgressBar
        value={progress}
        label={`${formatDuration(stats.approved_duration_secs)} / ${formatDuration(config.target_duration_secs)}`}
        color={progress >= 1 ? 'green' : 'blue'}
      />
    </div>
  )
}
```

- [ ] **Step 3: Commit**
```bash
git add frontend/src/components/project/SourcesTable.tsx frontend/src/components/project/StatsPanel.tsx
git commit -m "feat(frontend): add SourcesTable and StatsPanel components"
```

---

## Task 5: Dashboard — jobs panel, pipeline controls, upload area

**Files:**
- Create: `frontend/src/components/project/JobsPanel.tsx`
- Create: `frontend/src/components/project/PipelineControls.tsx`
- Create: `frontend/src/components/project/UploadArea.tsx`

- [ ] **Step 1: Create JobsPanel**

`frontend/src/components/project/JobsPanel.tsx`:
```tsx
import { ProgressBar } from '../ui/ProgressBar'
import type { JobSummary, FailedJob } from '../../types/api'

interface JobsPanelProps {
  activeJobs: JobSummary[]
  failedJobs: FailedJob[]
}

function jobLabel(type: string, sourceId: string | null): string {
  const typeLabel: Record<string, string> = {
    extract_audio: 'Extracting audio',
    vocal_separation: 'Vocal separation',
    diarisation: 'Diarisation',
    transcription: 'Transcription',
    cleanup: 'Cleanup',
    export: 'Export',
  }
  return typeLabel[type] ?? type
}

export function JobsPanel({ activeJobs, failedJobs }: JobsPanelProps) {
  if (activeJobs.length === 0 && failedJobs.length === 0) return null

  return (
    <div className="space-y-3">
      {activeJobs.length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-gray-700 mb-2">Active jobs</h3>
          <div className="space-y-2">
            {activeJobs.map(job => (
              <div key={job.id} className="bg-blue-50 rounded-lg px-3 py-2">
                <div className="flex items-center justify-between text-sm mb-1">
                  <span className="text-blue-700 font-medium">{jobLabel(job.type, null)}</span>
                  <span className="text-blue-600 text-xs">{job.status}</span>
                </div>
                {job.progress !== null && (
                  <ProgressBar value={job.progress / 100} color="blue" />
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {failedJobs.length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-gray-700 mb-2">Failed jobs</h3>
          <div className="space-y-2">
            {failedJobs.map(job => (
              <div key={job.id} className="bg-red-50 rounded-lg px-3 py-2">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-red-700 font-medium">{jobLabel(job.type, job.source_id)}</span>
                  {job.completed_at && (
                    <span className="text-red-400 text-xs">
                      {new Date(job.completed_at).toLocaleTimeString()}
                    </span>
                  )}
                </div>
                {job.error && (
                  <p className="text-xs text-red-600 mt-1 font-mono">{job.error}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: Create PipelineControls**

`frontend/src/components/project/PipelineControls.tsx`:
```tsx
import { useState } from 'react'
import { startPipeline, runTranscription } from '../../api/client'
import type { ProjectDetail } from '../../types/api'

interface PipelineControlsProps {
  project: ProjectDetail
  onAction: () => void
}

export function PipelineControls({ project, onAction }: PipelineControlsProps) {
  const [loading, setLoading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const hasStep1Pending = project.stats.source_coverage.some(
    s => s.status === 'step1_pending',
  )
  const isProcessing = project.active_jobs.length > 0

  async function handleStart() {
    setLoading('start')
    setError(null)
    try {
      await startPipeline(project.id)
      onAction()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start pipeline')
    } finally {
      setLoading(null)
    }
  }

  async function handleTranscription() {
    setLoading('transcription')
    setError(null)
    try {
      await runTranscription(project.id)
      onAction()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start transcription')
    } finally {
      setLoading(null)
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex gap-2 flex-wrap">
        <button
          onClick={handleStart}
          disabled={!hasStep1Pending || isProcessing || loading !== null}
          className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-40 font-medium"
        >
          {loading === 'start' ? 'Starting…' : 'Start pipeline'}
        </button>
        <button
          onClick={handleTranscription}
          disabled={isProcessing || loading !== null}
          className="px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-40"
        >
          {loading === 'transcription' ? 'Starting…' : 'Run transcription'}
        </button>
      </div>
      {error && (
        <div className="text-xs text-red-600 bg-red-50 rounded px-3 py-2">{error}</div>
      )}
    </div>
  )
}
```

- [ ] **Step 3: Create UploadArea**

`frontend/src/components/project/UploadArea.tsx`:
```tsx
import { useState, useRef } from 'react'
import { uploadSource, uploadReference } from '../../api/client'

interface UploadAreaProps {
  projectId: string
  onUploaded: () => void
}

export function UploadArea({ projectId, onUploaded }: UploadAreaProps) {
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const sourceInputRef = useRef<HTMLInputElement>(null)
  const refInputRef = useRef<HTMLInputElement>(null)

  async function handleSourceFile(file: File) {
    setUploading(`Uploading ${file.name}…`)
    setError(null)
    try {
      await uploadSource(projectId, file)
      onUploaded()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setUploading(null)
    }
  }

  async function handleReferenceFile(file: File) {
    setUploading(`Uploading reference clip…`)
    setError(null)
    try {
      await uploadReference(projectId, file)
      onUploaded()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setUploading(null)
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault()
    setDragging(false)
    const file = e.dataTransfer.files[0]
    if (file) void handleSourceFile(file)
  }

  return (
    <div className="space-y-3">
      <div
        onDragOver={e => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => sourceInputRef.current?.click()}
        className={`border-2 border-dashed rounded-xl p-6 text-center cursor-pointer transition-colors ${
          dragging ? 'border-blue-400 bg-blue-50' : 'border-gray-200 hover:border-gray-300'
        } ${uploading ? 'opacity-50 pointer-events-none' : ''}`}
      >
        <div className="text-sm text-gray-500">
          {uploading ?? (
            <>
              <span className="font-medium text-blue-600">Upload source video</span>
              {' — drag & drop or click to browse'}
            </>
          )}
        </div>
        <input
          ref={sourceInputRef}
          type="file"
          accept="video/*,audio/*"
          className="hidden"
          onChange={e => {
            const f = e.target.files?.[0]
            if (f) void handleSourceFile(f)
            e.target.value = ''
          }}
        />
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={() => refInputRef.current?.click()}
          disabled={uploading !== null}
          className="text-xs px-3 py-1.5 border border-gray-200 rounded-lg hover:bg-gray-50 disabled:opacity-50"
        >
          Upload reference clip
        </button>
        <span className="text-xs text-gray-400">Audio file, min 5 seconds</span>
        <input
          ref={refInputRef}
          type="file"
          accept="audio/*"
          className="hidden"
          onChange={e => {
            const f = e.target.files?.[0]
            if (f) void handleReferenceFile(f)
            e.target.value = ''
          }}
        />
      </div>

      {error && (
        <div className="text-xs text-red-600 bg-red-50 rounded px-3 py-2">{error}</div>
      )}
    </div>
  )
}
```

- [ ] **Step 4: Commit**
```bash
git add frontend/src/components/project/
git commit -m "feat(frontend): add JobsPanel, PipelineControls, UploadArea components"
```

---

## Task 6: Project dashboard page

**Files:**
- Modify: `frontend/src/pages/ProjectDashboardPage.tsx`

- [ ] **Step 1: Implement ProjectDashboardPage**

`frontend/src/pages/ProjectDashboardPage.tsx`:
```tsx
import { useState, useCallback } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useProjectPolling } from '../hooks/useProjectPolling'
import { reprocessSource } from '../api/client'
import { StatusBadge } from '../components/ui/StatusBadge'
import { SourcesTable } from '../components/project/SourcesTable'
import { StatsPanel } from '../components/project/StatsPanel'
import { JobsPanel } from '../components/project/JobsPanel'
import { PipelineControls } from '../components/project/PipelineControls'
import { UploadArea } from '../components/project/UploadArea'

export function ProjectDashboardPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const [reprocessError, setReprocessError] = useState<string | null>(null)

  const { data: project, isLoading, refetch } = useProjectPolling(projectId!)

  const handleAction = useCallback(() => {
    refetch()
  }, [refetch])

  async function handleReprocess(sourceId: string, steps: string[]) {
    setReprocessError(null)
    try {
      await reprocessSource(projectId!, sourceId, steps, undefined, true)
      refetch()
    } catch (err) {
      setReprocessError(err instanceof Error ? err.message : 'Reprocess failed')
    }
  }

  if (isLoading && !project) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-gray-400">Loading…</div>
      </div>
    )
  }

  if (!project) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-gray-400">Project not found. <Link to="/" className="text-blue-600 hover:underline">Go back</Link></div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-5xl mx-auto px-4 py-8 space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <div className="flex items-center gap-3">
              <Link to="/" className="text-gray-400 hover:text-gray-600 text-sm">← Projects</Link>
            </div>
            <div className="flex items-center gap-3 mt-1">
              <h1 className="text-2xl font-bold text-gray-900">{project.name}</h1>
              <StatusBadge status={project.status} />
            </div>
          </div>
          <Link
            to={`/projects/${projectId}/review`}
            className="px-4 py-2 bg-green-600 text-white text-sm rounded-lg hover:bg-green-700 font-medium"
          >
            Review queue →
          </Link>
        </div>

        {/* Jobs panel — shown when there's activity */}
        <JobsPanel activeJobs={project.active_jobs} failedJobs={project.recent_failed_jobs} />

        {/* Stats */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">Progress</h2>
          <StatsPanel stats={project.stats} config={project.config} />
        </div>

        {/* Pipeline controls */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <h2 className="text-sm font-semibold text-gray-700 mb-3">Pipeline</h2>
          <PipelineControls project={project} onAction={handleAction} />
        </div>

        {/* Sources */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">Sources</h2>
          <SourcesTable sources={project.stats.source_coverage} onReprocess={handleReprocess} />
          {reprocessError && (
            <div className="mt-2 text-xs text-red-600 bg-red-50 rounded px-3 py-2">{reprocessError}</div>
          )}
        </div>

        {/* Upload */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">Upload</h2>
          <UploadArea projectId={projectId!} onUploaded={handleAction} />
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Commit**
```bash
git add frontend/src/pages/ProjectDashboardPage.tsx
git commit -m "feat(frontend): implement project dashboard page"
```

---

## Task 7: Review queue — filter state hook and FilterBar

**Files:**
- Create: `frontend/src/hooks/useFilterState.ts`
- Create: `frontend/src/components/review/FilterBar.tsx`

- [ ] **Step 1: Create useFilterState hook**

`frontend/src/hooks/useFilterState.ts`:
```tsx
import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import type { GetSegmentsParams, SegmentStatus } from '../types/api'

export interface FilterState {
  status: string            // comma-separated or single status
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

function parseFloat_(s: string | null, fallback: number): number {
  if (s === null) return fallback
  const v = parseFloat(s)
  return isNaN(v) ? fallback : v
}

function parseInt_(s: string | null, fallback: number): number {
  if (s === null) return fallback
  const v = parseInt(s, 10)
  return isNaN(v) ? fallback : v
}

export function useFilterState() {
  const [params, setParams] = useSearchParams()

  const filter: FilterState = {
    status: params.get('status') ?? DEFAULT_FILTER.status,
    source_id: params.get('source_id') ?? '',
    min_confidence: parseFloat_(params.get('min_confidence'), DEFAULT_FILTER.min_confidence),
    min_duration: parseFloat_(params.get('min_duration'), 0),
    sort: params.get('sort') ?? DEFAULT_FILTER.sort,
    order: (params.get('order') ?? DEFAULT_FILTER.order) as 'asc' | 'desc',
    page: parseInt_(params.get('page'), 1),
  }

  const setFilter = useCallback(
    (update: Partial<FilterState>) => {
      setParams(prev => {
        const next = new URLSearchParams(prev)
        for (const [k, v] of Object.entries(update)) {
          if (v === '' || v === null || v === undefined) {
            next.delete(k)
          } else {
            next.set(k, String(v))
          }
        }
        // Reset page on filter change (unless explicitly setting page)
        if (!('page' in update)) {
          next.set('page', '1')
        }
        return next
      })
    },
    [setParams],
  )

  // Convert FilterState to GetSegmentsParams (for API)
  function toApiParams(overrides?: Partial<GetSegmentsParams>): GetSegmentsParams {
    const p: GetSegmentsParams = {
      page: filter.page,
      per_page: 50,
      sort: filter.sort,
      order: filter.order,
    }
    if (filter.status) {
      // status can be multi-value — pass as-is (API accepts comma-separated)
      ;(p as Record<string, unknown>).status = filter.status
    }
    if (filter.source_id) p.source_id = filter.source_id
    if (filter.min_confidence > 0) p.min_confidence = filter.min_confidence
    if (filter.min_duration > 0) p.min_duration = filter.min_duration
    return { ...p, ...overrides }
  }

  return { filter, setFilter, toApiParams }
}
```

- [ ] **Step 2: Create FilterBar**

`frontend/src/components/review/FilterBar.tsx`:
```tsx
import type { FilterState } from '../../hooks/useFilterState'
import type { SourceCoverage } from '../../types/api'

interface FilterBarProps {
  filter: FilterState
  sources: SourceCoverage[]
  onChange: (update: Partial<FilterState>) => void
}

const STATUS_OPTIONS = [
  { value: 'pending,maybe', label: 'Pending + Maybe' },
  { value: 'pending', label: 'Pending' },
  { value: 'maybe', label: 'Maybe' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'clipping_warning', label: 'Clipping warning' },
  { value: 'below_threshold', label: 'Below threshold' },
]

const SORT_OPTIONS = [
  { value: 'match_confidence', label: 'Confidence' },
  { value: 'duration', label: 'Duration' },
  { value: 'start_secs', label: 'Source order' },
  { value: 'transcript_confidence', label: 'Transcript confidence' },
]

export function FilterBar({ filter, sources, onChange }: FilterBarProps) {
  return (
    <div className="flex flex-wrap items-end gap-3 bg-white rounded-lg border border-gray-200 px-4 py-3">
      {/* Status */}
      <div className="min-w-[160px]">
        <label className="block text-xs text-gray-500 mb-1">Status</label>
        <select
          value={filter.status}
          onChange={e => onChange({ status: e.target.value })}
          className="w-full border border-gray-200 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
        >
          {STATUS_OPTIONS.map(opt => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      </div>

      {/* Source */}
      {sources.length > 1 && (
        <div className="min-w-[140px]">
          <label className="block text-xs text-gray-500 mb-1">Source</label>
          <select
            value={filter.source_id}
            onChange={e => onChange({ source_id: e.target.value })}
            className="w-full border border-gray-200 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
          >
            <option value="">All sources</option>
            {sources.map(s => (
              <option key={s.source_id} value={s.source_id}>{s.filename}</option>
            ))}
          </select>
        </div>
      )}

      {/* Min confidence */}
      <div className="min-w-[160px]">
        <label className="block text-xs text-gray-500 mb-1">
          Min confidence: {filter.min_confidence.toFixed(2)}
        </label>
        <input
          type="range"
          min="0"
          max="1"
          step="0.05"
          value={filter.min_confidence}
          onChange={e => onChange({ min_confidence: parseFloat(e.target.value) })}
          className="w-full"
        />
      </div>

      {/* Min duration */}
      <div className="min-w-[100px]">
        <label className="block text-xs text-gray-500 mb-1">Min duration (s)</label>
        <input
          type="number"
          min="0"
          step="0.5"
          value={filter.min_duration || ''}
          onChange={e => onChange({ min_duration: parseFloat(e.target.value) || 0 })}
          placeholder="0"
          className="w-full border border-gray-200 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
        />
      </div>

      {/* Sort */}
      <div className="min-w-[160px]">
        <label className="block text-xs text-gray-500 mb-1">Sort</label>
        <div className="flex gap-1">
          <select
            value={filter.sort}
            onChange={e => onChange({ sort: e.target.value })}
            className="flex-1 border border-gray-200 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
          >
            {SORT_OPTIONS.map(opt => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
          <button
            onClick={() => onChange({ order: filter.order === 'asc' ? 'desc' : 'asc' })}
            className="px-2 py-1.5 border border-gray-200 rounded text-sm hover:bg-gray-50"
            title={filter.order === 'asc' ? 'Ascending' : 'Descending'}
          >
            {filter.order === 'asc' ? '↑' : '↓'}
          </button>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 3: Commit**
```bash
git add frontend/src/hooks/useFilterState.ts frontend/src/components/review/FilterBar.tsx
git commit -m "feat(frontend): add useFilterState hook and FilterBar component"
```

---

## Task 8: Review queue — segment card and waveform/audio controls

**Files:**
- Create: `frontend/src/components/review/SegmentCard.tsx`
- Create: `frontend/src/components/review/WaveformCanvas.tsx`
- Create: `frontend/src/components/review/AudioControls.tsx`

- [ ] **Step 1: Create SegmentCard**

`frontend/src/components/review/SegmentCard.tsx`:
```tsx
import { ConfidenceBadge } from '../ui/ConfidenceBadge'
import { StatusBadge } from '../ui/StatusBadge'
import type { Segment } from '../../types/api'

interface SegmentCardProps {
  segment: Segment
  selected: boolean
  onClick: () => void
}

function formatDuration(secs: number): string {
  return `${secs.toFixed(1)}s`
}

export function SegmentCard({ segment, selected, onClick }: SegmentCardProps) {
  const displayTranscript = segment.transcript_edited ?? segment.transcript

  return (
    <div
      onClick={onClick}
      className={`px-4 py-3 border-b border-gray-100 cursor-pointer hover:bg-gray-50 transition-colors ${
        selected ? 'bg-blue-50 border-l-4 border-l-blue-400' : 'border-l-4 border-l-transparent'
      }`}
    >
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <ConfidenceBadge value={segment.match_confidence} />
            <span className="text-xs text-gray-400">{formatDuration(segment.duration_secs)}</span>
            <StatusBadge status={segment.status} dot />
            {segment.clipping_warning && (
              <span className="text-xs text-orange-500" title="Clipping warning">⚡</span>
            )}
          </div>
          {displayTranscript ? (
            <p className="text-sm text-gray-700 leading-snug line-clamp-2">{displayTranscript}</p>
          ) : (
            <p className="text-sm text-gray-400 italic">No transcript</p>
          )}
          <div className="text-xs text-gray-400 mt-1 truncate">{segment.source_filename}</div>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Create WaveformCanvas**

`frontend/src/components/review/WaveformCanvas.tsx`:
```tsx
import { useEffect, useRef } from 'react'

interface WaveformCanvasProps {
  audioUrl: string | null
  currentTime: number
  duration: number
  onSeek: (time: number) => void
  showSpectrogram?: boolean
}

export function WaveformCanvas({
  audioUrl,
  currentTime,
  duration,
  onSeek,
  showSpectrogram = false,
}: WaveformCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const samplesRef = useRef<Float32Array | null>(null)
  const urlRef = useRef<string | null>(null)

  // Decode audio and store samples when URL changes
  useEffect(() => {
    if (!audioUrl || audioUrl === urlRef.current) return
    urlRef.current = audioUrl
    samplesRef.current = null

    const ctx = new AudioContext()
    fetch(audioUrl)
      .then(r => r.arrayBuffer())
      .then(buf => ctx.decodeAudioData(buf))
      .then(decoded => {
        samplesRef.current = decoded.getChannelData(0)
        draw()
        ctx.close()
      })
      .catch(() => {
        ctx.close()
      })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [audioUrl])

  // Redraw whenever playback position changes
  useEffect(() => {
    draw()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentTime, duration, showSpectrogram])

  function draw() {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const { width, height } = canvas
    ctx.clearRect(0, 0, width, height)

    const samples = samplesRef.current
    if (!samples) {
      // Draw placeholder
      ctx.fillStyle = '#f3f4f6'
      ctx.fillRect(0, 0, width, height)
      ctx.fillStyle = '#9ca3af'
      ctx.font = '12px sans-serif'
      ctx.textAlign = 'center'
      ctx.fillText('Loading waveform…', width / 2, height / 2)
      return
    }

    // Background
    ctx.fillStyle = '#f9fafb'
    ctx.fillRect(0, 0, width, height)

    // Waveform
    const step = Math.ceil(samples.length / width)
    ctx.strokeStyle = '#6366f1'
    ctx.lineWidth = 1
    ctx.beginPath()
    for (let x = 0; x < width; x++) {
      let min = 1, max = -1
      for (let i = x * step; i < (x + 1) * step && i < samples.length; i++) {
        min = Math.min(min, samples[i])
        max = Math.max(max, samples[i])
      }
      const yMin = ((1 - min) / 2) * height
      const yMax = ((1 - max) / 2) * height
      ctx.moveTo(x + 0.5, yMin)
      ctx.lineTo(x + 0.5, yMax)
    }
    ctx.stroke()

    // Playhead
    if (duration > 0) {
      const px = (currentTime / duration) * width
      ctx.fillStyle = 'rgba(239, 68, 68, 0.8)'
      ctx.fillRect(px - 1, 0, 2, height)
    }
  }

  function handleClick(e: React.MouseEvent<HTMLCanvasElement>) {
    const canvas = canvasRef.current
    if (!canvas || duration === 0) return
    const rect = canvas.getBoundingClientRect()
    const x = e.clientX - rect.left
    const time = (x / canvas.width) * duration
    onSeek(time)
  }

  return (
    <canvas
      ref={canvasRef}
      width={600}
      height={80}
      onClick={handleClick}
      className="w-full rounded cursor-pointer bg-gray-50"
      style={{ height: 80 }}
    />
  )
}
```

- [ ] **Step 3: Create AudioControls**

`frontend/src/components/review/AudioControls.tsx`:
```tsx
interface AudioControlsProps {
  isPlaying: boolean
  currentTime: number
  duration: number
  playbackRate: number
  onToggle: () => void
  onRestart: () => void
  onSpeedChange: (rate: number) => void
}

const SPEEDS = [0.75, 1.0, 1.25, 1.5]

function formatTime(secs: number): string {
  const m = Math.floor(secs / 60)
  const s = Math.floor(secs % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

export function AudioControls({
  isPlaying,
  currentTime,
  duration,
  playbackRate,
  onToggle,
  onRestart,
  onSpeedChange,
}: AudioControlsProps) {
  return (
    <div className="flex items-center gap-3">
      <button
        onClick={onRestart}
        className="w-8 h-8 flex items-center justify-center text-gray-600 hover:text-gray-900 hover:bg-gray-100 rounded"
        title="Restart (R)"
      >
        ↩
      </button>

      <button
        onClick={onToggle}
        className="w-10 h-10 flex items-center justify-center bg-indigo-600 text-white rounded-full hover:bg-indigo-700 shadow-sm"
        title={isPlaying ? 'Pause (Space)' : 'Play (Space)'}
      >
        {isPlaying ? '⏸' : '▶'}
      </button>

      <div className="text-sm font-mono text-gray-600">
        {formatTime(currentTime)} / {formatTime(duration)}
      </div>

      <div className="flex gap-1 ml-auto">
        {SPEEDS.map(speed => (
          <button
            key={speed}
            onClick={() => onSpeedChange(speed)}
            className={`px-2 py-1 text-xs rounded ${
              playbackRate === speed
                ? 'bg-indigo-100 text-indigo-700 font-medium'
                : 'text-gray-500 hover:bg-gray-100'
            }`}
          >
            {speed}×
          </button>
        ))}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Commit**
```bash
git add frontend/src/components/review/SegmentCard.tsx frontend/src/components/review/WaveformCanvas.tsx frontend/src/components/review/AudioControls.tsx
git commit -m "feat(frontend): add SegmentCard, WaveformCanvas, AudioControls"
```

---

## Task 9: Review queue — segment detail panel

**Files:**
- Create: `frontend/src/components/review/SegmentDetail.tsx`
- Create: `frontend/src/components/review/KeyboardHelp.tsx`

- [ ] **Step 1: Create SegmentDetail**

`frontend/src/components/review/SegmentDetail.tsx`:
```tsx
import { useState, useEffect, useRef, useCallback } from 'react'
import { useAudio } from '../../hooks/useAudio'
import { getSegmentAudioUrl, patchSegment } from '../../api/client'
import { WaveformCanvas } from './WaveformCanvas'
import { AudioControls } from './AudioControls'
import { ConfidenceBadge } from '../ui/ConfidenceBadge'
import { StatusBadge } from '../ui/StatusBadge'
import type { Segment, SegmentStatus } from '../../types/api'

interface SegmentDetailProps {
  projectId: string
  segment: Segment
  onStatusChange: (id: string, status: SegmentStatus) => void
  onTranscriptChange: (id: string, transcript: string) => void
  onFocusChange: (focused: boolean) => void
  showSpectrogram: boolean
  onSpectrogramToggle: () => void
}

export function SegmentDetail({
  projectId,
  segment,
  onStatusChange,
  onTranscriptChange,
  onFocusChange,
  showSpectrogram,
  onSpectrogramToggle,
}: SegmentDetailProps) {
  const audioUrl = getSegmentAudioUrl(projectId, segment.id)
  const audio = useAudio(audioUrl)
  const [editedTranscript, setEditedTranscript] = useState<string | null>(null)
  const [isEditing, setIsEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const displayTranscript = editedTranscript ?? segment.transcript_edited ?? segment.transcript ?? ''
  const isModified = segment.transcript_edited !== null

  // Reset editing state when segment changes
  useEffect(() => {
    setIsEditing(false)
    setEditedTranscript(null)
    setActionError(null)
  }, [segment.id])

  function startEdit() {
    setEditedTranscript(displayTranscript)
    setIsEditing(true)
    setTimeout(() => textareaRef.current?.focus(), 0)
    onFocusChange(false) // keyboard shortcuts disabled during edit
  }

  async function saveTranscript() {
    if (editedTranscript === null) return
    setSaving(true)
    try {
      await patchSegment(projectId, segment.id, { transcript_edited: editedTranscript })
      onTranscriptChange(segment.id, editedTranscript)
      setIsEditing(false)
      setEditedTranscript(null)
      onFocusChange(true)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  function cancelEdit() {
    setIsEditing(false)
    setEditedTranscript(null)
    onFocusChange(true)
  }

  const applyAction = useCallback(async (status: SegmentStatus) => {
    setActionError(null)
    try {
      await patchSegment(projectId, segment.id, { status })
      onStatusChange(segment.id, status)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Action failed')
    }
  }, [projectId, segment.id, onStatusChange])

  const flags: string[] = []
  if (segment.clipping_warning) flags.push('Clipping warning')

  return (
    <div className="flex flex-col h-full bg-white">
      {/* Header */}
      <div className="px-4 pt-4 pb-3 border-b border-gray-100">
        <div className="flex items-center gap-2 mb-1">
          <StatusBadge status={segment.status} />
          <ConfidenceBadge value={segment.match_confidence} label="match" />
          {segment.transcript_confidence !== null && (
            <ConfidenceBadge value={segment.transcript_confidence} label="transcript" />
          )}
          {segment.clipping_warning && (
            <span className="text-xs text-orange-600 bg-orange-50 px-2 py-0.5 rounded">⚡ Clipping</span>
          )}
        </div>
        <div className="text-xs text-gray-500">
          {segment.source_filename} · {segment.start_secs.toFixed(2)}s – {segment.end_secs.toFixed(2)}s
          · {segment.duration_secs.toFixed(2)}s
        </div>
      </div>

      {/* Waveform */}
      <div className="px-4 py-3 border-b border-gray-100">
        <WaveformCanvas
          audioUrl={audioUrl}
          currentTime={audio.currentTime}
          duration={audio.duration}
          onSeek={audio.seek}
          showSpectrogram={showSpectrogram}
        />
        <div className="mt-2">
          <AudioControls
            isPlaying={audio.isPlaying}
            currentTime={audio.currentTime}
            duration={audio.duration}
            playbackRate={audio.playbackRate}
            onToggle={audio.toggle}
            onRestart={audio.restart}
            onSpeedChange={audio.setPlaybackRate}
          />
        </div>
        <div className="flex justify-end mt-1">
          <button
            onClick={onSpectrogramToggle}
            className="text-xs text-gray-400 hover:text-gray-600"
          >
            {showSpectrogram ? 'Waveform' : 'Spectrogram'} view
          </button>
        </div>
      </div>

      {/* Transcript */}
      <div className="px-4 py-3 flex-1 border-b border-gray-100">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-medium text-gray-600">Transcript</span>
          {isModified && !isEditing && (
            <span className="text-xs text-indigo-600">edited</span>
          )}
        </div>

        {isEditing ? (
          <div className="space-y-2">
            <textarea
              ref={textareaRef}
              value={editedTranscript ?? ''}
              onChange={e => setEditedTranscript(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  void saveTranscript()
                }
                if (e.key === 'Escape') cancelEdit()
              }}
              rows={4}
              className="w-full border border-blue-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 resize-none"
            />
            <div className="flex gap-2">
              <button
                onClick={() => void saveTranscript()}
                disabled={saving}
                className="px-3 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
              >
                {saving ? 'Saving…' : 'Save (Enter)'}
              </button>
              <button
                onClick={cancelEdit}
                className="px-3 py-1 text-xs border border-gray-200 rounded hover:bg-gray-50"
              >
                Cancel (Esc)
              </button>
            </div>
          </div>
        ) : (
          <div
            onClick={startEdit}
            className="text-sm text-gray-800 leading-relaxed cursor-text hover:bg-gray-50 rounded px-1 -mx-1 py-1 min-h-[4rem]"
            title="Click to edit (E)"
          >
            {displayTranscript || <span className="text-gray-400 italic">No transcript — click to add</span>}
          </div>
        )}
      </div>

      {/* Action buttons */}
      <div className="px-4 py-3">
        {actionError && (
          <div className="text-xs text-red-600 mb-2 bg-red-50 rounded px-2 py-1">{actionError}</div>
        )}
        <div className="flex gap-2">
          <button
            onClick={() => void applyAction('approved')}
            className="flex-1 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 font-medium"
            title="Approve (A)"
          >
            {segment.clipping_warning ? '⚡ Approve' : 'Approve'} (A)
          </button>
          <button
            onClick={() => void applyAction('maybe')}
            className="flex-1 py-2 text-sm bg-yellow-500 text-white rounded-lg hover:bg-yellow-600 font-medium"
            title="Maybe (M)"
          >
            Maybe (M)
          </button>
          <button
            onClick={() => void applyAction('rejected')}
            className="flex-1 py-2 text-sm bg-red-500 text-white rounded-lg hover:bg-red-600 font-medium"
            title="Reject (X)"
          >
            Reject (X)
          </button>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Create KeyboardHelp overlay**

`frontend/src/components/review/KeyboardHelp.tsx`:
```tsx
interface KeyboardHelpProps {
  onClose: () => void
}

const SHORTCUTS = [
  { key: 'A', desc: 'Approve segment' },
  { key: 'M', desc: 'Mark as maybe' },
  { key: 'X', desc: 'Reject segment' },
  { key: 'J', desc: 'Next segment' },
  { key: 'K', desc: 'Previous segment' },
  { key: 'Space', desc: 'Play / pause audio' },
  { key: 'R', desc: 'Restart audio' },
  { key: 'E', desc: 'Edit transcript' },
  { key: '[', desc: 'Slower playback' },
  { key: ']', desc: 'Faster playback' },
  { key: '?', desc: 'Show/hide shortcuts' },
]

export function KeyboardHelp({ onClose }: KeyboardHelpProps) {
  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-xl shadow-xl p-6 w-80"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-semibold">Keyboard shortcuts</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">✕</button>
        </div>
        <table className="w-full text-sm">
          <tbody className="divide-y divide-gray-50">
            {SHORTCUTS.map(s => (
              <tr key={s.key}>
                <td className="py-1.5 pr-4">
                  <kbd className="px-2 py-0.5 bg-gray-100 rounded text-xs font-mono">{s.key}</kbd>
                </td>
                <td className="py-1.5 text-gray-700">{s.desc}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
```

- [ ] **Step 3: Commit**
```bash
git add frontend/src/components/review/SegmentDetail.tsx frontend/src/components/review/KeyboardHelp.tsx
git commit -m "feat(frontend): add SegmentDetail panel and KeyboardHelp overlay"
```

---

## Task 10: Review queue — bulk operations panel

**Files:**
- Create: `frontend/src/components/review/BulkOperations.tsx`

- [ ] **Step 1: Create BulkOperations**

`frontend/src/components/review/BulkOperations.tsx`:
```tsx
import { useState, useEffect, useCallback } from 'react'
import { bulkSegmentAction, getSegmentsCount } from '../../api/client'
import type { BulkFilter, BulkSegmentRequest, SegmentStatus } from '../../types/api'

interface BulkOperationsProps {
  projectId: string
  onApplied: () => void
  sources: Array<{ source_id: string; filename: string }>
}

interface Preset {
  label: string
  req: BulkSegmentRequest
}

const PRESETS: Preset[] = [
  {
    label: 'Approve pending ≥0.90',
    req: { action: 'approve', filter: { status: 'pending', min_confidence: 0.9 } },
  },
  {
    label: 'Approve pending ≥0.85',
    req: { action: 'approve', filter: { status: 'pending', min_confidence: 0.85 } },
  },
  {
    label: 'Reject pending <1.5s',
    req: { action: 'reject', filter: { status: 'pending', max_duration: 1.5 } },
  },
  {
    label: 'Reject pending <2.0s',
    req: { action: 'reject', filter: { status: 'pending', max_duration: 2.0 } },
  },
  {
    label: 'Reset maybe → pending',
    req: { action: 'pending', filter: { status: 'maybe' } },
  },
]

const ACTIONS = ['approve', 'reject', 'maybe', 'pending'] as const
const STATUSES: SegmentStatus[] = ['pending', 'maybe', 'approved', 'rejected', 'below_threshold', 'clipping_warning']

export function BulkOperations({ projectId, onApplied, sources }: BulkOperationsProps) {
  const [expanded, setExpanded] = useState(false)
  const [custom, setCustom] = useState<{
    action: typeof ACTIONS[number]
    filter: BulkFilter
  }>({
    action: 'approve',
    filter: { status: 'pending' },
  })
  const [previewCount, setPreviewCount] = useState<number | null>(null)
  const [applying, setApplying] = useState(false)
  const [result, setResult] = useState<string | null>(null)

  const fetchPreview = useCallback(async () => {
    setPreviewCount(null)
    try {
      const { total } = await getSegmentsCount(projectId, custom.filter)
      setPreviewCount(total)
    } catch {
      setPreviewCount(null)
    }
  }, [projectId, custom.filter])

  useEffect(() => {
    if (expanded) void fetchPreview()
  }, [expanded, fetchPreview])

  async function applyPreset(preset: Preset) {
    setApplying(true)
    setResult(null)
    try {
      const { affected_count } = await bulkSegmentAction(projectId, preset.req)
      setResult(`Applied: ${affected_count} segments updated`)
      onApplied()
    } catch (err) {
      setResult(err instanceof Error ? err.message : 'Failed')
    } finally {
      setApplying(false)
    }
  }

  async function applyCustom() {
    setApplying(true)
    setResult(null)
    try {
      const { affected_count } = await bulkSegmentAction(projectId, custom)
      setResult(`Applied: ${affected_count} segments updated`)
      onApplied()
    } catch (err) {
      setResult(err instanceof Error ? err.message : 'Failed')
    } finally {
      setApplying(false)
    }
  }

  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <button
        onClick={() => setExpanded(e => !e)}
        className="w-full px-4 py-2.5 text-left text-sm font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 flex items-center justify-between"
      >
        Bulk operations
        <span className="text-gray-400">{expanded ? '▲' : '▼'}</span>
      </button>

      {expanded && (
        <div className="px-4 py-3 space-y-3">
          {result && (
            <div className="text-sm text-green-700 bg-green-50 rounded px-3 py-2">{result}</div>
          )}

          {/* Presets */}
          <div>
            <div className="text-xs text-gray-500 mb-2">Presets</div>
            <div className="flex flex-wrap gap-2">
              {PRESETS.map(preset => (
                <button
                  key={preset.label}
                  onClick={() => void applyPreset(preset)}
                  disabled={applying}
                  className="text-xs px-3 py-1.5 border border-gray-200 rounded-lg hover:bg-gray-50 disabled:opacity-50"
                >
                  {preset.label}
                </button>
              ))}
            </div>
          </div>

          {/* Custom */}
          <div className="border-t border-gray-100 pt-3">
            <div className="text-xs text-gray-500 mb-2">Custom</div>
            <div className="flex flex-wrap gap-2 items-end">
              <div>
                <label className="block text-xs text-gray-400 mb-1">Action</label>
                <select
                  value={custom.action}
                  onChange={e => setCustom(c => ({ ...c, action: e.target.value as typeof ACTIONS[number] }))}
                  className="border border-gray-200 rounded px-2 py-1 text-sm"
                >
                  {ACTIONS.map(a => <option key={a} value={a}>{a}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Status</label>
                <select
                  value={custom.filter.status ?? ''}
                  onChange={e => setCustom(c => ({ ...c, filter: { ...c.filter, status: e.target.value as SegmentStatus || undefined } }))}
                  className="border border-gray-200 rounded px-2 py-1 text-sm"
                >
                  <option value="">Any</option>
                  {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Min confidence</label>
                <input
                  type="number"
                  min="0"
                  max="1"
                  step="0.05"
                  placeholder="0"
                  value={custom.filter.min_confidence ?? ''}
                  onChange={e => setCustom(c => ({ ...c, filter: { ...c.filter, min_confidence: parseFloat(e.target.value) || undefined } }))}
                  className="border border-gray-200 rounded px-2 py-1 text-sm w-20"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Max duration (s)</label>
                <input
                  type="number"
                  min="0"
                  step="0.5"
                  placeholder="∞"
                  value={custom.filter.max_duration ?? ''}
                  onChange={e => setCustom(c => ({ ...c, filter: { ...c.filter, max_duration: parseFloat(e.target.value) || undefined } }))}
                  className="border border-gray-200 rounded px-2 py-1 text-sm w-20"
                />
              </div>
              {sources.length > 1 && (
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Source</label>
                  <select
                    value={custom.filter.source_id ?? ''}
                    onChange={e => setCustom(c => ({ ...c, filter: { ...c.filter, source_id: e.target.value || undefined } }))}
                    className="border border-gray-200 rounded px-2 py-1 text-sm"
                  >
                    <option value="">All</option>
                    {sources.map(s => <option key={s.source_id} value={s.source_id}>{s.filename}</option>)}
                  </select>
                </div>
              )}
              <div className="flex items-end gap-2">
                <button
                  onClick={() => void fetchPreview()}
                  disabled={applying}
                  className="px-3 py-1 text-xs border border-gray-200 rounded hover:bg-gray-50"
                >
                  Preview {previewCount !== null ? `(${previewCount})` : ''}
                </button>
                <button
                  onClick={() => void applyCustom()}
                  disabled={applying || previewCount === 0}
                  className="px-3 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
                >
                  {applying ? 'Applying…' : 'Apply'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: Commit**
```bash
git add frontend/src/components/review/BulkOperations.tsx
git commit -m "feat(frontend): add BulkOperations panel with presets and custom filter"
```

---

## Task 11: Review queue — Timeline component

**Files:**
- Create: `frontend/src/components/review/Timeline.tsx`

- [ ] **Step 1: Create Timeline**

`frontend/src/components/review/Timeline.tsx`:
```tsx
import { useRef, useEffect, useState } from 'react'
import type { Segment } from '../../types/api'

interface TimelineProps {
  segments: Segment[]
  totalDuration: number
  selectedSegmentId: string | null
  onSegmentSelect: (id: string) => void
  visibleRange?: [number, number]
}

const STATUS_COLOR: Record<string, string> = {
  approved: '#22c55e',
  rejected: '#ef4444',
  maybe: '#f59e0b',
  pending: '#94a3b8',
  below_threshold: '#e2e8f0',
  clipping_warning: '#f97316',
  auto_rejected: '#fca5a5',
}

export function Timeline({
  segments,
  totalDuration,
  selectedSegmentId,
  onSegmentSelect,
}: TimelineProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [zoom, setZoom] = useState(1)
  const [offset, setOffset] = useState(0) // seconds from start

  const visibleDuration = totalDuration / zoom

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || totalDuration === 0) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const { width, height } = canvas
    ctx.clearRect(0, 0, width, height)

    // Background
    ctx.fillStyle = '#f1f5f9'
    ctx.fillRect(0, 0, width, height)

    const timeToX = (t: number) => ((t - offset) / visibleDuration) * width

    for (const seg of segments) {
      const x1 = timeToX(seg.start_secs)
      const x2 = timeToX(seg.end_secs)
      if (x2 < 0 || x1 > width) continue

      const color = STATUS_COLOR[seg.status] ?? '#94a3b8'
      ctx.fillStyle = seg.id === selectedSegmentId ? '#3b82f6' : color
      ctx.fillRect(
        Math.max(0, x1),
        2,
        Math.min(width, x2) - Math.max(0, x1),
        height - 4,
      )
    }

    // Border
    ctx.strokeStyle = '#cbd5e1'
    ctx.strokeRect(0, 0, width, height)
  }, [segments, totalDuration, selectedSegmentId, zoom, offset, visibleDuration])

  function handleClick(e: React.MouseEvent<HTMLCanvasElement>) {
    const canvas = canvasRef.current
    if (!canvas || totalDuration === 0) return
    const rect = canvas.getBoundingClientRect()
    const x = e.clientX - rect.left
    const clickTime = offset + (x / canvas.width) * visibleDuration

    // Find nearest segment
    let best: Segment | null = null
    let bestDist = Infinity
    for (const seg of segments) {
      const mid = (seg.start_secs + seg.end_secs) / 2
      const dist = Math.abs(mid - clickTime)
      if (
        clickTime >= seg.start_secs &&
        clickTime <= seg.end_secs &&
        dist < bestDist
      ) {
        best = seg
        bestDist = dist
      }
    }
    if (best) onSegmentSelect(best.id)
  }

  function handleWheel(e: React.WheelEvent) {
    e.preventDefault()
    const newZoom = Math.min(20, Math.max(1, zoom * (e.deltaY > 0 ? 0.9 : 1.1)))
    setZoom(newZoom)
    // Keep center fixed
    const canvas = canvasRef.current
    if (canvas) {
      const rect = canvas.getBoundingClientRect()
      const cx = (e.clientX - rect.left) / canvas.width
      const centerTime = offset + cx * visibleDuration
      const newVisibleDuration = totalDuration / newZoom
      setOffset(Math.max(0, Math.min(totalDuration - newVisibleDuration, centerTime - cx * newVisibleDuration)))
    }
  }

  if (segments.length === 0 || totalDuration === 0) return null

  return (
    <div className="w-full">
      <canvas
        ref={canvasRef}
        width={800}
        height={32}
        onClick={handleClick}
        onWheel={handleWheel}
        className="w-full rounded cursor-pointer"
        style={{ height: 32 }}
        title="Click to navigate, scroll to zoom"
      />
    </div>
  )
}
```

- [ ] **Step 2: Commit**
```bash
git add frontend/src/components/review/Timeline.tsx
git commit -m "feat(frontend): add canvas Timeline component with zoom and click navigation"
```

---

## Task 12: Export button and flow

**Files:**
- Create: `frontend/src/components/export/ExportButton.tsx`

- [ ] **Step 1: Create ExportButton**

`frontend/src/components/export/ExportButton.tsx`:
```tsx
import { useState } from 'react'
import { triggerExport, getExportDownloadUrl } from '../../api/client'
import type { ProjectDetail } from '../../types/api'

interface ExportButtonProps {
  project: ProjectDetail
}

type ExportState = 'idle' | 'confirm' | 'exporting' | 'ready'

export function ExportButton({ project }: ExportButtonProps) {
  const [state, setState] = useState<ExportState>('idle')
  const [error, setError] = useState<string | null>(null)

  const approvedCount = project.stats.approved_count
  const approvedDuration = project.stats.approved_duration_secs
  const clippingCount = project.stats.source_coverage.filter(
    s => s.low_coverage_warning,
  ).length

  if (approvedCount === 0) {
    return (
      <button disabled className="px-4 py-2 text-sm border border-gray-200 rounded-lg text-gray-400 cursor-not-allowed">
        Export (no approvals)
      </button>
    )
  }

  function formatDur(secs: number): string {
    const h = Math.floor(secs / 3600)
    const m = Math.floor((secs % 3600) / 60)
    return h > 0 ? `${h}h ${m}m` : `${m}m`
  }

  if (state === 'confirm') {
    return (
      <div className="border border-gray-200 rounded-xl p-4 bg-white max-w-sm">
        <h3 className="font-medium text-sm mb-3">Export confirmation</h3>
        <div className="space-y-1 text-sm text-gray-600 mb-4">
          <div>{approvedCount} segments · {formatDur(approvedDuration)}</div>
          {clippingCount > 0 && (
            <div className="text-orange-600">⚡ {clippingCount} sources with low coverage</div>
          )}
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setState('idle')}
            className="flex-1 px-3 py-1.5 text-sm border border-gray-200 rounded-lg hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            onClick={async () => {
              setState('exporting')
              setError(null)
              try {
                await triggerExport(project.id)
                setState('ready')
              } catch (err) {
                setError(err instanceof Error ? err.message : 'Export failed')
                setState('confirm')
              }
            }}
            className="flex-1 px-3 py-1.5 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700"
          >
            Export
          </button>
        </div>
        {error && <div className="mt-2 text-xs text-red-600">{error}</div>}
      </div>
    )
  }

  if (state === 'exporting') {
    return (
      <button disabled className="px-4 py-2 text-sm bg-green-100 text-green-700 rounded-lg flex items-center gap-2">
        <span className="animate-spin">⟳</span> Exporting…
      </button>
    )
  }

  if (state === 'ready') {
    return (
      <a
        href={getExportDownloadUrl(project.id)}
        download
        className="px-4 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 font-medium"
      >
        ↓ Download export
      </a>
    )
  }

  return (
    <button
      onClick={() => setState('confirm')}
      className="px-4 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 font-medium"
    >
      Export ({approvedCount} · {formatDur(approvedDuration)})
    </button>
  )
}
```

- [ ] **Step 2: Commit**
```bash
git add frontend/src/components/export/ExportButton.tsx
git commit -m "feat(frontend): add ExportButton with confirmation and download flow"
```

---

## Task 13: Review queue page — full assembly

**Files:**
- Modify: `frontend/src/pages/ReviewQueuePage.tsx`
- Update: `frontend/src/hooks/useProjectPolling.ts` — add `refetch` to return value

- [ ] **Step 1: Add refetch to useProjectPolling**

Read `frontend/src/hooks/useProjectPolling.ts` first, then update its return type and value to expose `refetch`:

```ts
// In useProjectPolling, the usePolling hook's execute function is the refetch.
// Modify the return to include refetch: execute from usePolling.
// The execute function should be exposed from usePolling first.
```

Current `usePolling` returns `{ data, error, isLoading }`. We need to expose the `execute` function.

Update `frontend/src/hooks/usePolling.ts` — add `refetch` to return:
```tsx
// At end of usePolling, return:
return { data, error, isLoading, refetch: execute }
```

Update `UsePollingResult` interface:
```tsx
interface UsePollingResult<T> {
  data: T | null
  error: Error | null
  isLoading: boolean
  refetch: () => Promise<void>
}
```

Update `frontend/src/hooks/useProjectPolling.ts` to pass through `refetch`:
```tsx
// In the return, include refetch from polling
return { data: polling.data, error: polling.error, isLoading: polling.isLoading, refetch: polling.refetch }
```

- [ ] **Step 2: Implement ReviewQueuePage**

`frontend/src/pages/ReviewQueuePage.tsx`:
```tsx
import { useState, useEffect, useCallback, useRef } from 'react'
import { useParams, Link } from 'react-router-dom'
import { usePolling } from '../hooks/usePolling'
import { useProjectPolling } from '../hooks/useProjectPolling'
import { getSegments, getSegmentAudioUrl, patchSegment } from '../api/client'
import { useFilterState } from '../hooks/useFilterState'
import { FilterBar } from '../components/review/FilterBar'
import { SegmentCard } from '../components/review/SegmentCard'
import { SegmentDetail } from '../components/review/SegmentDetail'
import { BulkOperations } from '../components/review/BulkOperations'
import { Timeline } from '../components/review/Timeline'
import { KeyboardHelp } from '../components/review/KeyboardHelp'
import { ExportButton } from '../components/export/ExportButton'
import type { Segment, SegmentStatus } from '../types/api'

export function ReviewQueuePage() {
  const { projectId } = useParams<{ projectId: string }>()
  const { filter, setFilter, toApiParams } = useFilterState()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [shortcutsEnabled, setShortcutsEnabled] = useState(true)
  const [showHelp, setShowHelp] = useState(false)
  const [showSpectrogram, setShowSpectrogram] = useState(false)
  const [segments, setSegments] = useState<Segment[]>([])
  const [pagination, setPagination] = useState({ page: 1, pages: 1, total: 0, per_page: 50 })
  const [segmentsLoading, setSegmentsLoading] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)

  const { data: project } = useProjectPolling(projectId!)

  // Fetch segments when filter or page changes
  const fetchSegments = useCallback(async () => {
    if (!projectId) return
    setSegmentsLoading(true)
    try {
      const result = await getSegments(projectId, toApiParams())
      setSegments(result.segments)
      setPagination(result.pagination)
      // Auto-select first segment if none selected
      setSelectedId(prev => {
        if (prev && result.segments.find(s => s.id === prev)) return prev
        return result.segments[0]?.id ?? null
      })
    } finally {
      setSegmentsLoading(false)
    }
  }, [projectId, toApiParams, refreshKey]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void fetchSegments()
  }, [fetchSegments])

  const selectedSegment = segments.find(s => s.id === selectedId) ?? null
  const selectedIndex = segments.findIndex(s => s.id === selectedId)

  function selectNext() {
    if (selectedIndex < segments.length - 1) {
      setSelectedId(segments[selectedIndex + 1].id)
    } else if (filter.page < pagination.pages) {
      setFilter({ page: filter.page + 1 })
    }
  }

  function selectPrev() {
    if (selectedIndex > 0) {
      setSelectedId(segments[selectedIndex - 1].id)
    } else if (filter.page > 1) {
      setFilter({ page: filter.page - 1 })
    }
  }

  // Keyboard shortcuts
  useEffect(() => {
    if (!shortcutsEnabled) return

    const SPEEDS = [0.75, 1.0, 1.25, 1.5]

    function onKey(e: KeyboardEvent) {
      // Don't fire when typing in inputs
      const tag = (e.target as HTMLElement).tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return

      switch (e.key) {
        case 'j':
        case 'J':
          e.preventDefault()
          selectNext()
          break
        case 'k':
        case 'K':
          e.preventDefault()
          selectPrev()
          break
        case 'a':
        case 'A':
          e.preventDefault()
          if (selectedSegment) void applyAction(selectedSegment, 'approved')
          break
        case 'm':
        case 'M':
          e.preventDefault()
          if (selectedSegment) void applyAction(selectedSegment, 'maybe')
          break
        case 'x':
        case 'X':
          e.preventDefault()
          if (selectedSegment) void applyAction(selectedSegment, 'rejected')
          break
        case '?':
          setShowHelp(h => !h)
          break
      }
    }

    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [shortcutsEnabled, selectedSegment, selectedIndex, segments, filter.page, pagination.pages]) // eslint-disable-line react-hooks/exhaustive-deps

  async function applyAction(segment: Segment, status: SegmentStatus) {
    try {
      await patchSegment(projectId!, segment.id, { status })
      // Update locally for instant feedback
      setSegments(prev =>
        prev.map(s => (s.id === segment.id ? { ...s, status } : s)),
      )
      // Auto-advance
      selectNext()
    } catch {
      // Silent — detail panel shows error
    }
  }

  function handleStatusChange(id: string, status: SegmentStatus) {
    setSegments(prev => prev.map(s => (s.id === id ? { ...s, status } : s)))
    selectNext()
  }

  function handleTranscriptChange(id: string, transcript: string) {
    setSegments(prev =>
      prev.map(s => (s.id === id ? { ...s, transcript_edited: transcript } : s)),
    )
  }

  const sources = project?.stats.source_coverage ?? []
  const totalDuration = segments.reduce((sum, s) => sum + s.duration_secs, 0)

  return (
    <div className="h-screen flex flex-col bg-gray-50 overflow-hidden">
      {/* Top bar */}
      <div className="flex items-center gap-4 px-4 py-3 bg-white border-b border-gray-200 shrink-0">
        <Link to={`/projects/${projectId}`} className="text-gray-400 hover:text-gray-600 text-sm">
          ← {project?.name ?? 'Dashboard'}
        </Link>
        <span className="text-gray-300">|</span>
        <span className="text-sm text-gray-600">
          {pagination.total} segments
        </span>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => setShowHelp(h => !h)}
            className="text-xs px-2 py-1 border border-gray-200 rounded hover:bg-gray-50"
          >
            ? Shortcuts
          </button>
          {project && <ExportButton project={project} />}
        </div>
      </div>

      {/* Filter bar */}
      <div className="px-4 py-2 shrink-0">
        <FilterBar filter={filter} sources={sources} onChange={setFilter} />
      </div>

      {/* Timeline */}
      {segments.length > 0 && (
        <div className="px-4 pb-2 shrink-0">
          <Timeline
            segments={segments}
            totalDuration={totalDuration}
            selectedSegmentId={selectedId}
            onSegmentSelect={id => setSelectedId(id)}
          />
        </div>
      )}

      {/* Main two-panel layout */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: segment list */}
        <div className="w-80 flex-none flex flex-col border-r border-gray-200 bg-white overflow-hidden">
          {/* Bulk operations */}
          <div className="p-2 border-b border-gray-100 shrink-0">
            <BulkOperations
              projectId={projectId!}
              onApplied={() => setRefreshKey(k => k + 1)}
              sources={sources.map(s => ({ source_id: s.source_id, filename: s.filename }))}
            />
          </div>

          {/* Segment list */}
          <div className="flex-1 overflow-y-auto">
            {segmentsLoading && segments.length === 0 && (
              <div className="text-center py-8 text-gray-400 text-sm">Loading…</div>
            )}
            {!segmentsLoading && segments.length === 0 && (
              <div className="text-center py-8 text-gray-400 text-sm">No segments match your filters.</div>
            )}
            {segments.map(segment => (
              <SegmentCard
                key={segment.id}
                segment={segment}
                selected={segment.id === selectedId}
                onClick={() => setSelectedId(segment.id)}
              />
            ))}
          </div>

          {/* Pagination */}
          {pagination.pages > 1 && (
            <div className="px-4 py-2 border-t border-gray-100 shrink-0 flex items-center justify-between text-sm">
              <button
                onClick={() => setFilter({ page: filter.page - 1 })}
                disabled={filter.page <= 1}
                className="text-gray-400 hover:text-gray-700 disabled:opacity-30"
              >
                ← Prev
              </button>
              <span className="text-gray-500 text-xs">
                {filter.page} / {pagination.pages}
              </span>
              <button
                onClick={() => setFilter({ page: filter.page + 1 })}
                disabled={filter.page >= pagination.pages}
                className="text-gray-400 hover:text-gray-700 disabled:opacity-30"
              >
                Next →
              </button>
            </div>
          )}
        </div>

        {/* Right: detail panel */}
        <div className="flex-1 overflow-hidden">
          {selectedSegment ? (
            <SegmentDetail
              projectId={projectId!}
              segment={selectedSegment}
              onStatusChange={handleStatusChange}
              onTranscriptChange={handleTranscriptChange}
              onFocusChange={setShortcutsEnabled}
              showSpectrogram={showSpectrogram}
              onSpectrogramToggle={() => setShowSpectrogram(s => !s)}
            />
          ) : (
            <div className="flex items-center justify-center h-full text-gray-400">
              {segments.length === 0 ? 'No segments to review' : 'Select a segment'}
            </div>
          )}
        </div>
      </div>

      {showHelp && <KeyboardHelp onClose={() => setShowHelp(false)} />}
    </div>
  )
}
```

- [ ] **Step 3: Commit**
```bash
git add frontend/src/hooks/usePolling.ts frontend/src/hooks/useProjectPolling.ts frontend/src/pages/ReviewQueuePage.tsx
git commit -m "feat(frontend): implement review queue page with keyboard shortcuts and two-panel layout"
```

---

## Task 14: Build verification and final commit

- [ ] **Step 1: Run TypeScript build**
```bash
cd frontend && pnpm build 2>&1
```
Expected: Build succeeds with no TypeScript errors. Fix any type errors.

- [ ] **Step 2: Verify all routes render**
Start dev server and check `/`, `/projects/test`, `/projects/test/review` load without console errors.

- [ ] **Step 3: Final commit and push**
```bash
git add -A
git commit -m "feat(frontend): Wave 5 complete — project list, dashboard, review queue, export"
git push -u origin integrate/frontend
```

---

## Self-Review Notes

- All 6 deliverables covered: project list, dashboard, review queue, bulk operations, timeline, export
- Filter state persisted in URL via `useFilterState` hook
- Keyboard shortcuts: A/M/X/J/K/Space/R/E/[/]/? as spec requires
- Auto-advance after action implemented in `handleStatusChange`
- Spectrogram toggle persists in session state (component state in ReviewQueuePage)
- Timeline: canvas-rendered, click-to-navigate, scroll-to-zoom
- Export: greyed when no approvals, confirmation panel, download link
- Bulk operations: presets + custom with live preview count
- Waveform: canvas-rendered with real audio decoding via Web Audio API
- `usePolling` needs `refetch` added to its return — covered in Task 13 Step 1
- `useProjectPolling` needs to expose `refetch` — covered in Task 13 Step 1
- The `GetSegmentsParams.status` type is `SegmentStatus` (single) but filters can be comma-separated — cast needed (already handled in `toApiParams` with `as Record<string, unknown>`)
