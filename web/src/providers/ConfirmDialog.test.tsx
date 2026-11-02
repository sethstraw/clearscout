import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConfirmationProvider, useConfirmation } from "../confirmations";
import { ConfirmDialog } from "./ConfirmDialog";

const options = {
  title: "Archive this job?",
  description: "Archiving keeps its history and takes it out of the ledger.",
  confirmLabel: "Archive it",
};

afterEach(() => vi.restoreAllMocks());

describe("ConfirmDialog", () => {
  it("wraps through disclosure summaries rather than their hidden links", () => {
    render(
      <ConfirmDialog
        onCancel={() => undefined}
        onConfirm={() => undefined}
        options={{
          ...options,
          details: (
            <details>
              <summary>Price details</summary>
              <a href="https://example.test/prices">Prices</a>
            </details>
          ),
        }}
      />,
    );
    const summary = screen.getByText("Price details");
    screen.getByRole("button", { name: options.confirmLabel }).focus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(summary);
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: options.confirmLabel }));
  });
  it("announces itself as a modal dialog named by its own heading", () => {
    render(
      <ConfirmDialog onCancel={() => undefined} onConfirm={() => undefined} options={options} />,
    );
    const dialog = screen.getByRole("dialog");

    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(screen.getByRole("heading", { name: options.title }).id).toBe(
      dialog.getAttribute("aria-labelledby"),
    );
    expect(screen.getByText(options.description).id).toBe(dialog.getAttribute("aria-describedby"));
  });

  it("opens on the cancelling button, so a stray Enter keeps the record", () => {
    render(
      <ConfirmDialog onCancel={() => undefined} onConfirm={() => undefined} options={options} />,
    );
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
  });

  it("cancels on Escape without confirming", () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(<ConfirmDialog onCancel={onCancel} onConfirm={onConfirm} options={options} />);

    fireEvent.keyDown(window, { key: "Escape" });

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("keeps Tab inside the dialog in both directions", () => {
    render(
      <ConfirmDialog onCancel={() => undefined} onConfirm={() => undefined} options={options} />,
    );
    const keep = screen.getByRole("button", { name: "Cancel" });
    const confirm = screen.getByRole("button", { name: options.confirmLabel });

    confirm.focus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(keep);

    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(confirm);
  });

  it("returns focus to the button that opened it", () => {
    render(<button type="button">Archive</button>);
    const trigger = screen.getByRole("button", { name: "Archive" });
    trigger.focus();
    const { unmount } = render(
      <ConfirmDialog onCancel={() => undefined} onConfirm={() => undefined} options={options} />,
    );

    unmount();

    expect(document.activeElement).toBe(trigger);
  });

  it.each([
    ["disables itself", (trigger: HTMLButtonElement) => (trigger.disabled = true)],
    ["is removed", (trigger: HTMLButtonElement) => trigger.remove()],
  ])("moves focus to the button's own region, not the body, when its button %s", (_, change) => {
    render(
      <main id="main-content" tabIndex={-1}>
        <section aria-label="Archived jobs" tabIndex={-1}>
          <button type="button">Archive</button>
        </section>
      </main>,
    );
    const trigger = screen.getByRole<HTMLButtonElement>("button", { name: "Archive" });
    trigger.focus();
    const { unmount } = render(
      <ConfirmDialog onCancel={() => undefined} onConfirm={() => undefined} options={options} />,
    );

    change(trigger);
    unmount();

    expect(document.activeElement).toBe(screen.getByRole("region", { name: "Archived jobs" }));
  });

  it("moves focus to the page, not the body, when nothing held focus as it opened", () => {
    render(<main id="main-content" tabIndex={-1} />);
    const { unmount } = render(
      <ConfirmDialog onCancel={() => undefined} onConfirm={() => undefined} options={options} />,
    );

    unmount();

    expect(document.activeElement).toBe(document.getElementById("main-content"));
  });

  it("marks a destructive confirmation differently from an ordinary one", () => {
    const { unmount } = render(
      <ConfirmDialog onCancel={() => undefined} onConfirm={() => undefined} options={options} />,
    );
    expect(screen.getByRole("button", { name: options.confirmLabel }).className).toContain(
      "button--primary",
    );
    unmount();

    render(
      <ConfirmDialog
        onCancel={() => undefined}
        onConfirm={() => undefined}
        options={{ ...options, danger: true }}
      />,
    );
    expect(screen.getByRole("button", { name: options.confirmLabel }).className).toContain(
      "button--danger-solid",
    );
  });
});

function Caller() {
  const confirm = useConfirmation();
  const [answer, setAnswer] = useState("nothing asked");
  return (
    <>
      <button
        onClick={() => void confirm(options).then((ok) => setAnswer(String(ok)))}
        type="button"
      >
        Archive
      </button>
      <p>{answer}</p>
    </>
  );
}

describe("the confirmation seam", () => {
  it("resolves true only when the destructive button is pressed", async () => {
    render(
      <ConfirmationProvider>
        <Caller />
      </ConfirmationProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Archive" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("false")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Archive" }));
    fireEvent.click(await screen.findByRole("button", { name: options.confirmLabel }));
    expect(await screen.findByText("true")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("refuses to run outside the provider rather than silently allowing the action", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    expect(() => render(<Caller />)).toThrow(/Confirmation context is unavailable/);
  });
});
