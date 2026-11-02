import { createContext, useContext, useId, useRef, useState } from "react";
import type { PropsWithChildren, ReactNode } from "react";

import { HELP, type HelpKey } from "../help/content";
import { HelpPanel } from "../help/HelpPanel";

const InsidePage = createContext(false);

/**
 * A routed page frame. A page mounted inside another page's tab renders as a
 * section under that page's heading, so one screen keeps one main landmark.
 */
export function Page({
  eyebrow,
  title,
  description,
  action,
  children,
  density = "default",
  help,
}: PropsWithChildren<{
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
  density?: "default" | "dense";
  help?: HelpKey;
}>) {
  const nested = useContext(InsidePage);
  const [helpOpen, setHelpOpen] = useState(false);
  const helpId = useId();
  const helpButton = useRef<HTMLButtonElement>(null);
  if (nested) {
    return (
      <section aria-label={title} className="page-section">
        {description || action ? (
          <header className="page-section-header">
            <p className="page-description">{description}</p>
            {action}
          </header>
        ) : null}
        {children}
      </section>
    );
  }
  return (
    <main
      className={`page${density === "dense" ? " page--dense" : ""}`}
      id="main-content"
      tabIndex={-1}
    >
      <header className="page-header">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h1>{title}</h1>
          <p className="page-description">{description}</p>
        </div>
        {help ? (
          <div className="page-header-actions">
            {action}
            <button
              aria-controls={helpOpen ? helpId : undefined}
              aria-expanded={helpOpen}
              aria-label={`Help: ${HELP[help].title}`}
              className="help-button"
              data-tour="page-help"
              onClick={() => setHelpOpen((open) => !open)}
              ref={helpButton}
              type="button"
            >
              ?
            </button>
          </div>
        ) : (
          action
        )}
      </header>
      {help && helpOpen && (
        <HelpPanel
          helpKey={help}
          id={helpId}
          onClose={() => {
            setHelpOpen(false);
            helpButton.current?.focus();
          }}
        />
      )}
      <InsidePage.Provider value={true}>
        <div className="page-body">{children}</div>
      </InsidePage.Provider>
    </main>
  );
}
