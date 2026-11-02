import type { HttpHandler } from "msw";

// Add per-test handlers with `server.use(...)` inside the test; the shared list
// stays empty so a test that forgets a mock fails loudly rather than hitting
// jsdom's fetch and getting an ambiguous error.
export const handlers: HttpHandler[] = [];
