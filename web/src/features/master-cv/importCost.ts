/** Which imports reach the model, read from the filename before upload. */

const TEXT_SUFFIXES = [".md", ".markdown", ".txt"];

/**
 * A text file in the canonical format is adopted without a model call. Any
 * other file is re-read by the model; the server refuses a text file that
 * needs one until the re-read is confirmed.
 */
export function needsModelRead(filename: string): boolean {
  const name = filename.toLowerCase();
  return !TEXT_SUFFIXES.some((suffix) => name.endsWith(suffix));
}
