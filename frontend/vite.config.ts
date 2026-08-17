import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

const backendPort = process.env.VITE_BACKEND_PORT || "8600"

export default defineConfig({
  // Vault is mounted at /vault/ behind the platform nginx. base ensures
  // vite emits asset URLs as /vault/assets/... so the browser fetches them
  // through the right nginx location instead of getting 404s at /assets/...
  base: "/vault/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      "/api": {
        target: `http://localhost:${backendPort}`,
        changeOrigin: true,
      },
      // The dev server serves the app under /vault/ (vite base) and
      // apiFetch prefixes BASE_URL, so proxy the prefixed form too,
      // stripping the prefix the way nginx does in prod.
      "/vault/api": {
        target: `http://localhost:${backendPort}`,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/vault/, ""),
      },
    },
  },
})
