import { useEffect, useId, useRef } from "react";

import { HELP, USER_GUIDE_URL, type HelpKey } from "./content";
import { startFindTour } from "./tour";

/** The page help opened from the header "?" button; Escape or Close dismisses it. */
export function HelpPanel({
  helpKey,
  id,
  onClose,
}: {
  helpKey: HelpKey;
  id: string;
  onClose: () => void;
}) {
  const entry: { title: string; body: string; docsAnchor?: string } = HELP[helpKey];
  const titleId = useId();
  const panel = useRef<HTMLElement>(null);
  useEffect(() => panel.current?.focus(), []);
  useEffect(() => {
    const element = panel.current;
    if (!element) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      onClose();
    };
    element.addEventListener("keydown", closeOnEscape);
    return () => element.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);
  return (
    <section aria-labelledby={titleId} className="help-panel" id={id} ref={panel} tabIndex={-1}>
      <h2 id={titleId}>{entry.title}</h2>
      <p>{entry.body}</p>
      <div className="help-panel-actions">
        {entry.docsAnchor && (
          <a href={`${USER_GUIDE_URL}#${entry.docsAnchor}`} rel="noreferrer" target="_blank">
            Read more in the user guide
          </a>
        )}
        {helpKey.startsWith("find.") && (
          <button
            className="button button--quiet"
            onClick={() => {
              onClose();
              void startFindTour();
            }}
            type="button"
          >
            Take the tour
          </button>
        )}
        <button className="button button--quiet" onClick={onClose} type="button">
          Close help
        </button>
      </div>
    </section>
  );
}
