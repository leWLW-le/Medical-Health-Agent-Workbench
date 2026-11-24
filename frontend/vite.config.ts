import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), ['HEALTH_GATEWAY_PROXY', 'HEALTH_PYTHON_PROXY', 'VITE_HEALTH_API_URL'])
  const envValue = (key: string) => process.env[key] || env[key]
  const proxy = {
    '/api/health': envValue('HEALTH_GATEWAY_PROXY') || 'http://127.0.0.1:8080',
    '^/api/(?:chat|vision-chat|report-photo/session)(?:\\?|$)': envValue('HEALTH_PYTHON_PROXY') || envValue('VITE_HEALTH_API_URL') || 'http://127.0.0.1:8000',
  }
  return { plugins: [react()], server: { proxy }, preview: { proxy } }
})
