import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Otto-seeded (profile: webapp.react-vite-fastapi.py312). AUTHORITATIVE.
// The clean verifier injects PORT / API_PORT; never hard-code ports here.
const frontendPort = Number(
  process.env.PORT ?? process.env.FRONTEND_PORT ?? "5173",
);
const apiPort = process.env.API_PORT ?? "8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: frontendPort,
    strictPort: true,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${apiPort}`,
        changeOrigin: true,
      },
    },
  },
});
