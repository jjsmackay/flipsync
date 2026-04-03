import { useParams } from 'react-router-dom'

export function ReviewQueuePage() {
  const { projectId } = useParams<{ projectId: string }>()
  return (
    <div className="p-8">
      <h1 className="text-2xl font-bold">ReviewQueuePage</h1>
      <p className="text-gray-500 mt-2">
        Review queue for <code>{projectId}</code> — coming in Wave 5.
      </p>
    </div>
  )
}
