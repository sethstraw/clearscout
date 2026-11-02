export type FocusOrigin = { origin: HTMLElement | null; landing: HTMLElement | null };

/** The control that holds focus now, and the focusable region around it. */
export function captureFocusOrigin(): FocusOrigin {
  const active = document.activeElement;
  const origin = active instanceof HTMLElement && active !== document.body ? active : null;
  return { origin, landing: origin?.parentElement?.closest<HTMLElement>("[tabindex]") ?? null };
}

/** Focus the origin while it can take focus, else its region, else the page. */
export function returnFocus({ origin, landing }: FocusOrigin) {
  if (origin?.isConnected && !origin.matches(":disabled")) origin.focus();
  else (landing?.isConnected ? landing : document.getElementById("main-content"))?.focus();
}

/** Whether focus sits where a disabled or removed control dropped it, not on a new choice. */
export function focusIsParked({ landing }: FocusOrigin): boolean {
  const active = document.activeElement;
  return !active || active === document.body || active === landing || active.id === "main-content";
}
