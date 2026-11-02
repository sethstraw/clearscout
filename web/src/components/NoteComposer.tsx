export function NoteComposer({
  title,
  body,
  onTitle,
  onBody,
  onSubmit,
  rows = 8,
  saving = false,
  error,
}: {
  title: string;
  body: string;
  onTitle: (value: string) => void;
  onBody: (value: string) => void;
  onSubmit: () => void;
  rows?: number;
  saving?: boolean;
  error?: string | null;
}) {
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <label>
        Title
        <input onChange={(event) => onTitle(event.target.value)} required value={title} />
      </label>
      <label>
        Markdown note
        <textarea
          onChange={(event) => onBody(event.target.value)}
          required
          rows={rows}
          value={body}
        />
      </label>
      {error && <p className="field-error">{error}</p>}
      <button className="button button--primary" disabled={saving} type="submit">
        Save note
      </button>
    </form>
  );
}
