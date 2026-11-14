/** Backend HTTP base URL with trailing slashes stripped (the `/media` endpoint root). */
const MEDIA_BASE_URL = API_BASE_URL.replace(/\/+$/, '');

/** Content-addressed artifact references written by the backend media pipeline. */
export const ARTIFACT_URI_PREFIX = 'sherry-artifact://';

export interface ArtifactRef {
  sessionId: string;
  name: string;
}

/**
 * Parse a `sherry-artifact://<session_id>/<name>` reference.
 * @param uri Candidate reference
 * @returns The two path segments, or null when it is not an artifact URI
 */
export function parseArtifactUri(uri: string): ArtifactRef | null {
  if (!uri.startsWith(ARTIFACT_URI_PREFIX)) return null;
  const rest = uri.slice(ARTIFACT_URI_PREFIX.length);
  const slash = rest.indexOf('/');
  if (slash <= 0 || slash === rest.length - 1) return null;
  return { sessionId: rest.slice(0, slash), name: rest.slice(slash + 1) };
}

/**
 * Build the backend `/artifacts` URL for one stored artifact.
 * @param sessionId Session that owns the artifact
 * @param name Content-addressed file name
 */
export function artifactUrl(sessionId: string, name: string): string {
  return `${MEDIA_BASE_URL}/artifacts/${encodeURIComponent(sessionId)}/${encodeURIComponent(name)}`;
}

/**
 * Resolve an artifact reference to its served HTTP URL.
 * @param uri `sherry-artifact://` reference (returned verbatim by the message row)
 * @returns The HTTP URL, or null when the value is not an artifact reference
 */
export function artifactUriToHttp(uri: string): string | null {
  const ref = parseArtifactUri(uri);
  return ref ? artifactUrl(ref.sessionId, ref.name) : null;
}

/** One artifact reference extracted from free text plus its served URL. */
export interface ResolvedArtifactImage {
  /** The raw `sherry-artifact://` reference. */
  uri: string;
  /** The backend `/artifacts` URL an `<img>` can load. */
  url: string;
}

/**
 * Every artifact reference in free text (a tool result, a markdown answer).
 *
 * The match stops at whitespace, quotes, brackets and markdown punctuation, so
 * `![shot](sherry-artifact://…)`, a JSON-quoted URI and a bare line all yield
 * the same reference. Order is preserved and duplicates collapse: the same
 * image referenced twice renders once.
 * @param text Arbitrary text possibly containing artifact references
 * @returns The references in first-seen order, deduplicated
 */
export function extractArtifactUris(text: string | null | undefined): string[] {
  const source = text ?? '';
  if (!source.includes(ARTIFACT_URI_PREFIX)) return [];
  const seen = new Set<string>();
  const uris: string[] = [];
  for (const match of source.matchAll(/sherry-artifact:\/\/[^\s)\]}"'<>]+/g)) {
    if (!seen.has(match[0])) {
      seen.add(match[0]);
      uris.push(match[0]);
    }
  }
  return uris;
}

/**
 * Resolve every artifact image reference in free text to displayable images.
 * @param text Arbitrary text possibly containing artifact references
 * @returns One entry per resolvable reference (unresolvable ones are dropped)
 */
export function resolveArtifactImages(text: string | null | undefined): ResolvedArtifactImage[] {
  return extractArtifactUris(text).flatMap(uri => {
    const url = artifactUriToHttp(uri);
    return url ? [{ uri, url }] : [];
  });
}

/**
 * Build the backend `/media` URL for a persisted media file.
 *
 * Accepts a bare filename or a full persisted file path: the file may carry any
 * directory prefix, so only its basename is sent as the `filename` query param.
 * @param sessionId Session the media file belongs to
 * @param filePathOrName Persisted absolute file path (or bare filename)
 */
export function mediaUrl(sessionId: string | null | undefined, filePathOrName: string): string {
  const filename = filePathOrName.split(/[\\/]/).pop() || '';
  return `${MEDIA_BASE_URL}/media?session_id=${encodeURIComponent(sessionId ?? '')}&filename=${encodeURIComponent(filename)}`;
}

/**
 * Strip the `data:<mime>;base64,` prefix from a data URL, keeping only the base64 payload.
 * @param dataUrl
 */
export function extractBase64(dataUrl: string): string {
  return dataUrl.split(',')[1] ?? '';
}
