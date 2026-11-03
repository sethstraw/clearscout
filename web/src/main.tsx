import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { AppProviders } from "./providers";
import { App } from "./App";
import { ErrorBoundary } from "./providers/ErrorBoundary";
import { moveFormerStorageKeys } from "./storageKeys";
import "./styles.css";

for (const storage of [() => window.localStorage, () => window.sessionStorage]) {
  try {
    moveFormerStorageKeys(storage());
  } catch {
    // Blocked storage has nothing to move.
  }
}

const root = document.getElementById("root");
if (!root) throw new Error("ClearScout could not find its application root.");

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <BrowserRouter>
        <AppProviders>
          <App />
        </AppProviders>
      </BrowserRouter>
    </ErrorBoundary>
  </StrictMode>,
);
