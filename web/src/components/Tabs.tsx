import { useRef } from "react";
import type { KeyboardEvent, ReactNode } from "react";

type TabItem = { id: string; label: string };

export function Tabs({
  id,
  items,
  label,
  onSelect,
  selected,
}: {
  id: string;
  items: readonly TabItem[];
  label: string;
  onSelect: (id: string) => void;
  selected: string;
}) {
  const buttons = useRef<Array<HTMLButtonElement | null>>([]);
  const move = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    const target =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? items.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + items.length) % items.length;
    const item = items[target];
    if (item) onSelect(item.id);
    buttons.current[target]?.focus();
  };
  return (
    <div aria-label={label} className="tabs" role="tablist">
      {items.map((item, index) => (
        <button
          aria-controls={`${id}-panel-${item.id}`}
          aria-selected={selected === item.id}
          className={selected === item.id ? "active" : ""}
          id={`${id}-tab-${item.id}`}
          key={item.id}
          onClick={() => onSelect(item.id)}
          onKeyDown={(event) => move(event, index)}
          ref={(button) => {
            buttons.current[index] = button;
          }}
          role="tab"
          tabIndex={selected === item.id ? 0 : -1}
          type="button"
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

export function TabPanel({
  children,
  id,
  selected,
}: {
  children: ReactNode;
  id: string;
  selected: string;
}) {
  return (
    <section
      aria-labelledby={`${id}-tab-${selected}`}
      className="tab-panel"
      id={`${id}-panel-${selected}`}
      role="tabpanel"
      tabIndex={0}
    >
      {children}
    </section>
  );
}
