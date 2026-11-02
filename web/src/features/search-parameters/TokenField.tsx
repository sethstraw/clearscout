import { useId, useState } from "react";

import { Icons } from "../../components/Icons";

/** Each stored list value is entered as its own token. */
export function TokenField({
  hint,
  label,
  max,
  onChange,
  placeholder,
  values,
}: {
  hint: string;
  label: string;
  max?: number;
  onChange: (values: string[]) => void;
  placeholder: string;
  values: string[];
}) {
  const [entry, setEntry] = useState("");
  const inputId = useId();
  const full = max !== undefined && values.length >= max;
  const add = () => {
    const value = entry.trim();
    if (full || !value || values.includes(value)) return setEntry("");
    onChange([...values, value]);
    setEntry("");
  };
  // Siblings, not nested in a label: .compact-field's grid stacks them.
  return (
    <div className="compact-field token-field">
      <label htmlFor={inputId}>{label}</label>
      <input
        disabled={full}
        id={inputId}
        onChange={(event) => setEntry(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === ",") {
            event.preventDefault();
            add();
          }
        }}
        placeholder={placeholder}
        value={entry}
      />
      <small className="field-note">
        {hint}
        {max !== undefined &&
          (full ? ` Limit of ${max} reached; remove a term to add another.` : ` At most ${max}.`)}
      </small>
      {values.length > 0 && (
        <ul className="token-list">
          {values.map((value) => (
            <li key={value}>
              <span>{value}</span>
              <button
                aria-label={`Remove ${value}`}
                onClick={() => onChange(values.filter((item) => item !== value))}
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
