import { Component } from "react";
import type { PropsWithChildren, ReactNode } from "react";

import { BrandMark } from "../components/Brand";

type State = { error: Error | null };

export class ErrorBoundary extends Component<PropsWithChildren, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <main className="fatal-error" id="main-content" tabIndex={-1}>
        <BrandMark size="large" />
        <p className="eyebrow">Local interface error</p>
        <h1>This view could not be displayed.</h1>
        <p>
          Your durable records were not changed. Reload the app; if the problem returns, include the
          message below when reporting it.
        </p>
        <pre>{this.state.error.message}</pre>
        <button
          className="button button--primary"
          onClick={() => window.location.reload()}
          type="button"
        >
          Reload ClearScout
        </button>
      </main>
    );
  }
}
