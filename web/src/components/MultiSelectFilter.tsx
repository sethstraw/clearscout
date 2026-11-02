import { useEffect, useId, useRef } from "react";

type MultiSelectOption = { value: string; label: string; count: number };

/** A disclosure of checkboxes that narrows a list to any of the chosen values. */
export function MultiSelectFilter({
  allLabel,
  label,
  onChange,
  options,
  selected,
}: {
  allLabel: string;
  label: string;
  onChange: (next: string[]) => void;
  options: readonly MultiSelectOption[];
  selected: readonly string[];
}) {
  const captionId = useId();
  const disclosureRef = useRef<HTMLDetailsElement>(null);
  const toggleRef = useRef<HTMLElement>(null);
  // The panel overlays the table, so a press elsewhere or Escape from inside closes it.
  useEffect(() => {
    const closeOutside = (event: PointerEvent) => {
      const disclosure = disclosureRef.current;
      if (disclosure?.open && !disclosure.contains(event.target as Node)) disclosure.open = false;
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      const disclosure = disclosureRef.current;
      if (event.key !== "Escape" || !disclosure?.open) return;
      if (!disclosure.contains(event.target as Node)) return;
      disclosure.open = false;
      toggleRef.current?.focus();
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, []);
  const offered = new Set(options.map((option) => option.value));
  // A chosen value the other filters no longer count stays listed so it can be cleared.
  const listed = [
    ...options,
    ...selected
      .filter((value) => !offered.has(value))
      .map((value) => ({ value, label: value, count: 0 })),
  ];
  const summary =
    selected.length === 0
      ? allLabel
      : selected.length === 1
        ? selected[0]
        : `${selected.length} selected`;
  const toggle = (value: string, checked: boolean) =>
    onChange(checked ? [...selected, value] : selected.filter((item) => item !== value));

  return (
    <div className="multi-select">
      <span className="multi-select__caption" id={captionId}>
        {label}
      </span>
      <details ref={disclosureRef}>
        <summary ref={toggleRef}>
          <span className="visually-hidden">{label}: </span>
          {summary}
        </summary>
        <fieldset aria-labelledby={captionId} className="multi-select__panel">
          {selected.length > 0 && (
            <button
              className="button button--quiet"
              onClick={() => {
                onChange([]);
                toggleRef.current?.focus();
              }}
              type="button"
            >
              Clear {label.toLowerCase()} filter
            </button>
          )}
          {listed.length === 0 && <p className="field-note">Nothing to choose from.</p>}
          {listed.map((option) => (
            <label className="multi-select__option" key={option.value}>
              <input
                checked={selected.includes(option.value)}
                onChange={(event) => toggle(option.value, event.target.checked)}
                type="checkbox"
              />
              {option.label} ({option.count})
            </label>
          ))}
        </fieldset>
      </details>
    </div>
  );
}
