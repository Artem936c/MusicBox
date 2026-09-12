import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Конфигурация сборки Mini App.
// base: './' — относительные пути к ассетам, чтобы сборка работала из любого подкаталога
// (в т.ч. при монтировании статики FastAPI на '/').
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    // Ассеты кладём в подкаталог assets — так их проще отдавать статикой.
    assetsDir: 'assets',
    chunkSizeWarningLimit: 900,
  },
  server: {
    host: true,
    port: 5173,
    strictPort: false,
    // Запросы к API в режиме разработки проксируем на локальный backend,
    // поэтому фронтенд ходит по относительному пути '/api' без CORS.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: true,
    port: 4173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
