import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { ProjectListPage } from './pages/ProjectListPage'
import { ProjectDashboardPage } from './pages/ProjectDashboardPage'
import { ReviewQueuePage } from './pages/ReviewQueuePage'
import { QcPage } from './pages/QcPage'

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<ProjectListPage />} />
        <Route path="/projects/:projectId" element={<ProjectDashboardPage />} />
        <Route path="/projects/:projectId/review" element={<ReviewQueuePage />} />
        <Route path="/projects/:projectId/qc" element={<QcPage />} />
        {/* Fallback */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
