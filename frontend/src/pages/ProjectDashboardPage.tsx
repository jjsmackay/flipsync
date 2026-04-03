import { useParams } from 'react-router-dom'

export function ProjectDashboardPage() {
  const { projectId } = useParams<{ projectId: string }>()
  return (
    <div className="p-8">
      <h1 className="text-2xl font-bold">ProjectDashboardPage</h1>
      <p className="text-gray-500 mt-2">
        Project dashboard for <code>{projectId}</code> — coming in Wave 5.
      </p>
    </div>
  )
}
