import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies the API to `make serve` on port 8000.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/ask": "http://localhost:8000", "/health": "http://localhost:8000" } },
});
