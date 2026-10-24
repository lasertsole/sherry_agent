/**
 * File-preview format classification for the file viewer panel.
 *
 * The panel renders markdown through the sanitizing directive, PDF / docx /
 * images through their dedicated viewers, and everything else falls back to a
 * plain `<pre>` — the classifier is the single decision point in front of
 * those branches. Unknown (or known-unsupported, like legacy `.doc`) always
 * degrades to text: a wrong guess must never crash the panel.
 */

/** The panel's render branches. */
export type FilePreviewFormat = 'markdown' | 'pdf' | 'docx' | 'image' | 'text';

/** Image suffixes the `<img>` branch serves (a superset of the chat's media filter). */
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif']);

/**
 * Classify one project-file path for preview.
 * @param path The file's path relative to the project root.
 * @returns The branch to render; `text` is the fallback for everything the
 *   viewers do not cover.
 */
export function classifyFileFormat(path: string): FilePreviewFormat {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  if (ext === 'md' || ext === 'markdown') return 'markdown';
  if (ext === 'pdf') return 'pdf';
  // Legacy binary .doc is NOT the OOXML mammoth parses — it degrades to text.
  if (ext === 'docx') return 'docx';
  if (IMAGE_EXTENSIONS.has(ext)) return 'image';
  return 'text';
}
