import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// Built to dist/ and served by backend/server.py.
// In `npm run dev`, /api is proxied to the backend on VITE_API_PORT (default 8130).
export default defineConfig({
  plugins: [svelte()],
  base: './',
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    port: 5174,
    proxy: { '/api': `http://127.0.0.1:${process.env.VITE_API_PORT || 8130}` },
  },
})
