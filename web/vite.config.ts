import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export function isIneffectiveDynamicImportWarning(message: string) {
  return (
    message.includes("dynamically imported") &&
    message.includes("statically imported") &&
    message.includes("will not move")
  );
}

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      // Trailing slash keeps the SPA's /api-playground (and any other /api- route)
      // out of the proxy; only /api/... reaches the API server.
      "/api/": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true,
        // Rewrites Origin on every proxied /api/ request so any Origin-scoped
        // check on the API accepts the dev SPA served from a different port.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        configure: (proxy: any) => {
          // eslint-disable-next-line @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-member-access, @typescript-eslint/no-explicit-any
          proxy.on("proxyReq", (proxyReq: any) => {
            // eslint-disable-next-line @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-member-access
            proxyReq.setHeader("origin", "http://127.0.0.1:8765");
          });
        },
      },
      // The read-only model admin is served by the API, not the SPA, so the dev
      // SPA must proxy /admin or the "Data browser" link 404s. Keep the browser
      // Host (no changeOrigin) so starlette-admin asset URLs stay same-origin.
      "/admin": {
        target: "http://127.0.0.1:8765",
      },
    },
  },
  build: {
    sourcemap: false,
    rolldownOptions: {
      onLog(level, log, defaultHandler) {
        if (level === "warn" && isIneffectiveDynamicImportWarning(log.message)) {
          defaultHandler("error", log);
          return;
        }
        defaultHandler(level, log);
      },
      output: {
        codeSplitting: {
          groups: [
            { name: "charts", test: /node_modules[\\/]recharts|node_modules[\\/]d3-/ },
            {
              name: "editor",
              test: /node_modules[\\/]@uiw|node_modules[\\/]@codemirror|node_modules[\\/]codemirror/,
            },
            { name: "react", test: /node_modules[\\/]react|node_modules[\\/]scheduler/ },
          ],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    environmentOptions: { jsdom: { url: "http://127.0.0.1/" } },
    setupFiles: "./src/test/setup.ts",
  },
});
