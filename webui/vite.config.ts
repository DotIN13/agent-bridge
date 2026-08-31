import { defineConfig } from "vite";
import solid from "vite-plugin-solid";
import tailwindcss from "@tailwindcss/vite";

/** Where `npm run dev:server` listens. The same default the server picks. */
const SERVER = process.env.AB_WEBUI_SERVER ?? "http://127.0.0.1:8765";

export default defineConfig({
  root: ".",
  plugins: [solid(), tailwindcss()],
  // Two copies of solid-js would each own their own reactive graph.
  resolve: { dedupe: ["solid-js"] },
  server: {
    port: 8766,
    strictPort: true,
    // Loopback only, in dev as in production: this page can start ssh
    // processes and read the tokens behind them.
    host: "127.0.0.1",
    proxy: {
      "/api": { target: SERVER, changeOrigin: true },
      "/ws": { target: SERVER, ws: true },
    },
  },
  build: {
    target: "esnext",
    outDir: "dist/web",
    emptyOutDir: true,
    sourcemap: true,
  },
});
