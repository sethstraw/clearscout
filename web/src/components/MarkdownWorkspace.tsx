import MDEditor from "@uiw/react-md-editor/nohighlight";

/**
 * The editor is the library's, toolbar and preview included.
 *
 * Set data-color-mode from the app theme so the editor follows the in-app toggle.
 */
export function MarkdownWorkspace({
  content,
  onChange,
}: {
  content: string;
  onChange: (value: string) => void;
}) {
  const theme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  return (
    <div className="markdown-editor" data-color-mode={theme}>
      <MDEditor
        height={640}
        onChange={(value) => onChange(value ?? "")}
        value={content}
        visibleDragbar
      />
    </div>
  );
}
