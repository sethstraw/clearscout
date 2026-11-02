/** Explain the server-determined cost before import. */

const CANONICAL_SUFFIXES = [".md", ".markdown"];

export function importCostNote(filename: string): string {
  const markdown = CANONICAL_SUFFIXES.some((suffix) => filename.toLowerCase().endsWith(suffix));
  if (markdown) {
    return (
      "Markdown that already meets the canonical contract is adopted without a provider call. " +
      "Anything else is standardized by the configured provider and may incur model-token charges."
    );
  }
  return (
    "This file is standardized by the configured provider before it becomes canonical, " +
    "which may incur model-token charges."
  );
}
