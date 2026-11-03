import { afterAll, afterEach, beforeAll } from "vitest";
import { cleanup } from "@testing-library/react";

import { server } from "./msw/server";

// `onUnhandledFrame: "error"` makes an un-mocked fetch fail the test instead
// of returning a network error the assertion barely explains.
beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
afterAll(() => server.close());

const values = new Map<string, string>();
const localStorage: Storage = {
  get length() {
    return values.size;
  },
  clear: () => values.clear(),
  getItem: (key) => values.get(key) ?? null,
  key: (index) => [...values.keys()][index] ?? null,
  removeItem: (key) => {
    values.delete(key);
  },
  setItem: (key, value) => {
    values.set(key, value);
  },
};
Object.defineProperty(window, "localStorage", { configurable: true, value: localStorage });

// jsdom has no layout; suppress CodeMirror measurement noise in tests.
Range.prototype.getClientRects = () => [] as unknown as DOMRectList;

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  server.resetHandlers();
});
