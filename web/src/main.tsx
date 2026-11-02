import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { AppProviders } from "./providers";
import { App } from "./App";
import { ErrorBoundary } from "./providers/ErrorBoundary";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("TailorCV could not find its application root.");

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
