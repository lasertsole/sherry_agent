/**
 * The LSP configuration tab's API bridge (系统配置 → LSP / 代码智能).
 *
 * Session-scoped: the backend resolves the same project root the LSP tools use,
 * so the project layer edited here is the file those tools merge.
 */
export interface LspServerEntry {
  language: string;
  command: string[];
  extensions: string[];
  language_id: string;
  install_hint: string;
  local_install: string | null;
  /** builtin / skill:<name> / project */
  source: string;
  enabled: boolean;
  installed: boolean;
  binary_path: string;
}

export interface LspProjectServer {
  command: string[];
  extensions: string[];
  language_id?: string;
  install_hint?: string;
}

export interface LspProjectPayload {
  servers: Record<string, LspProjectServer>;
  enable: string[];
}

export interface LspConfigPayload {
  success: boolean;
  session_id: string;
  root: string;
  project_path: string;
  servers: LspServerEntry[];
  enabled: string[];
  project: LspProjectPayload;
  errors: string[];
}

export interface LspTestResult {
  success: boolean;
  ok: boolean;
  language?: string;
  installed?: boolean;
  binary?: string;
  message?: string;
}

/** Read the merged builtin + skill + project view for one session. */
export async function readLspConfig(sessionId: string): Promise<LspConfigPayload> {
  const query = new URLSearchParams({ session_id: sessionId, _ts: String(Date.now()) });
  const res = await fetchApi<LspConfigPayload>({
    url: `/lsp/config?${query.toString()}`,
    method: 'get'
  });
  if (res === null || res === undefined) throw new Error('GET /lsp/config failed');
  return res;
}

/** Validate and store the project layer. */
export async function writeLspProject(
  sessionId: string,
  payload: LspProjectPayload
): Promise<{ success: boolean; message?: string }> {
  const res = await fetchApi<{ success?: boolean; message?: string }>({
    url: '/lsp/config/project',
    method: 'put',
    opts: { session_id: sessionId, ...payload }
  });
  return { success: res?.success === true, message: res?.message };
}

/** Validate and store an uploaded lsp.json text. */
export async function uploadLspProject(
  sessionId: string,
  content: string
): Promise<{ success: boolean; message?: string }> {
  const res = await fetchApi<{ success?: boolean; message?: string }>({
    url: '/lsp/config/project',
    method: 'post',
    opts: { session_id: sessionId, content }
  });
  return { success: res?.success === true, message: res?.message };
}

/** Remove one language from the project layer (builtin/skill entries are never touched). */
export async function deleteLspLanguage(
  sessionId: string,
  language: string
): Promise<{ success: boolean; message?: string }> {
  const res = await fetchApi<{ success?: boolean; message?: string }>({
    url: '/lsp/config/project/language',
    method: 'delete',
    opts: { session_id: sessionId, language }
  });
  return { success: res?.success === true, message: res?.message };
}

/** Resolve the binary and complete a real initialize handshake. */
export async function testLspServer(sessionId: string, language: string): Promise<LspTestResult> {
  const res = await fetchApi<LspTestResult>({
    url: '/lsp/config/test',
    method: 'post',
    opts: { session_id: sessionId, language }
  });
  return res ?? { success: false, ok: false, message: 'request failed' };
}
