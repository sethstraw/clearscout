import { HttpResponse, http, type HttpHandler } from "msw/http";

import type { Health } from "../../api/client/types";

// Every PaidAction reads health for free mode, so a paid-mode health is the one
// default. Add other mocks per test with `server.use(...)`.
export const handlers: HttpHandler[] = [
  http.get("*/api/v1/health", () =>
    HttpResponse.json({
      status: "ok",
      version: "0.1.0",
      database_exists: true,
      database_reachable: true,
      schema_current: true,
      current_revision: "0001",
      head_revision: "0001",
      worker_available: true,
      free_mode: false,
    } satisfies Health),
  ),
];
