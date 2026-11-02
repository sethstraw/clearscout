import { useId, useState } from "react";

import type { components } from "../../api/schema";
import { Icons } from "../../components/Icons";

type TargetTitle = components["schemas"]["TargetTitle"];

/** Each title is entered as its own token and carries the track it belongs to. */
export function TargetTitles({
  lanes,
  basic = false,
  onChange,
  values,
}: {
  lanes: string[];
  basic?: boolean;
  onChange: (values: TargetTitle[]) => void;
  values: TargetTitle[];
}) {
  const [entry, setEntry] = useState("");
  const inputId = useId();
  const add = () => {
    const title = entry.trim();
    if (!title || values.some((item) => item.title === title)) return setEntry("");
    onChange([...values, { title, lane: null }]);
    setEntry("");
  };
  const relane = (title: string, lane: string) =>
    onChange(
      values.map((item) =>
        item.title === title ? { ...item, lane: (lane || null) as TargetTitle["lane"] } : item,
      ),
    );
  return (
    <div className="compact-field token-field">
      <label htmlFor={inputId}>Target titles</label>
      <input
        id={inputId}
        onChange={(event) => setEntry(event.target.value)}
        onKeyDown={(event) => {
          // Enter alone commits: a title can contain a comma, so comma must not split it.
          if (event.key === "Enter") {
            event.preventDefault();
            add();
          }
        }}
        placeholder="Type a title, then press Enter"
        value={entry}
      />
      <small className="field-note">
        Add at least one title before scanning employer boards. Individual posting links remain
        available without it.
        {!basic && " A track is optional, and coverage counts untracked titles on their own."}
      </small>
      {values.length > 0 && (
        <ul className="token-list">
          {values.map((item) => (
            <li key={item.title}>
              <span>{item.title}</span>
              {!basic && (
                <select
                  aria-label={`Track for ${item.title}`}
                  onChange={(event) => relane(item.title, event.target.value)}
                  value={item.lane ?? ""}
                >
                  <option value="">No track</option>
                  {lanes.map((lane) => (
                    <option key={lane} value={lane}>
                      {lane}
                    </option>
                  ))}
                </select>
              )}
              <button
                aria-label={`Remove ${item.title}`}
                onClick={() => onChange(values.filter((other) => other.title !== item.title))}
                type="button"
              >
                <Icons.close height={12} width={12} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
