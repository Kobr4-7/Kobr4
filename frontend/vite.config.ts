import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// En développement, l'API tourne sur :8000 (kobr4 server run).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", ws: true },
      "/healthz": "http://127.0.0.1:8000",
    },
  },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
});
