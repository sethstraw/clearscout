import MDEditor from "@uiw/react-md-editor/nohighlight";

/** The one markdown renderer, and the same one the editor previews with. */
export function MarkdownView({
  children,
  className,
  demoteH1 = false,
}: {
  children: string;
  className?: string;
  demoteH1?: boolean;
}) {
  const theme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  return (
    <div
      className={className ? `markdown-document ${className}` : "markdown-document"}
      data-color-mode={theme}
    >
      <MDEditor.Markdown components={demoteH1 ? { h1: "h2" } : undefined} source={children} />
    </div>
  );
}
