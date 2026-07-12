import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const allowedHosts = process.env.VITE_ALLOWED_HOSTS?.split(',').map((h) => h.trim()).filter(Boolean)

// The frontend serves the API under a same-origin /api path and proxies it to
// the orchestrator. Same-origin means no CORS and no mixed-content blocking
// when the UI is served over https behind a reverse proxy. In the container,
// ORCHESTRATOR_PROXY_TARGET points at the compose service; the default suits
// local dev with the orchestrator on localhost:8000.
const orchestratorTarget = process.env.ORCHESTRATOR_PROXY_TARGET || 'http://localhost:8000'

const proxy = {
  '/api': {
    target: orchestratorTarget,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ''),
  },
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 3000,
    host: true,
    allowedHosts,
    proxy,
  },
  preview: {
    port: 3000,
    host: true,
    allowedHosts,
    proxy,
  },
})
