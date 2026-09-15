import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // 显式绑定 IPv4：默认 localhost 在新版 Node 下解析为 IPv6 [::1]，
    // 会导致 http://127.0.0.1:5173 无法访问
    host: '127.0.0.1',
    port: 5173,
  },
})
