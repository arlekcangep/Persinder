import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// API остаётся на stdlib-сервере Persinder (порт 8756). В разработке Vite
// проксирует те же пути, что и в будущей раздаче статики, — клиент всегда
// ходит на относительные /search и /health.
const API_TARGET = process.env.PERSINDER_API ?? "http://127.0.0.1:8756";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/search": { target: API_TARGET, changeOrigin: true },
      "/health": { target: API_TARGET, changeOrigin: true },
      "/ingest": { target: API_TARGET, changeOrigin: true },
    },
  },
});
