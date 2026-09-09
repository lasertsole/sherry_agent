/**
 * System prompt files CRUD (workspace persona files).
 *
 * @module bridge/systemPrompt
 */
import type { PromptFileResponse } from '~/types/backend/PromptFileResponse';
import { invokeNative } from './transport';

/**
 * Read all system prompt files.
 */
export async function readSystemPrompt(): Promise<Record<string, string>> {
  const native = await invokeNative<PromptFileResponse>('system_prompt_read');
  if (native !== null) return native.value.file_to_content;
  return fetchApi({ url: '/system_prompt', method: 'get' }) as unknown as Promise<Record<string, string>>;
}

/**
 * Read the persona template files for a given language (e.g. restore-default).
 *
 * The templates live under `workspace/template/<lang>/`. When `lang` is omitted
 * the backend falls back to the user's preferred workspace template language.
 * @param lang
 */
export async function readSystemPromptTemplate(lang?: string): Promise<Record<string, string>> {
  const native = await invokeNative<PromptFileResponse>('system_prompt_read_template', {
    payload: { lang: lang ?? null }
  });
  if (native !== null) return native.value.file_to_content;
  return fetchApi({
    url: '/system_prompt/template',
    opts: { lang: lang ?? undefined },
    method: 'get'
  }) as unknown as Promise<Record<string, string>>;
}

/**
 * Overwrite system prompt files (full replacement).
 * @param fileToContent
 */
export async function writeSystemPrompt(fileToContent: Record<string, string>): Promise<void> {
  const native = await invokeNative('system_prompt_write', { payload: { file_to_content: fileToContent } });
  if (native === null) {
    await fetchApi({
      url: '/system_prompt',
      opts: { file_to_content: fileToContent },
      method: 'put'
    });
  }
}

/**
 * Partially update system prompt files (merge).
 * @param fileToContent
 */
export async function updateSystemPrompt(fileToContent: Record<string, string>): Promise<void> {
  const native = await invokeNative('system_prompt_update', { payload: { file_to_content: fileToContent } });
  if (native === null) {
    await fetchApi({
      url: '/system_prompt',
      opts: { file_to_content: fileToContent },
      method: 'put'
    });
  }
}
