/**
 * Shiki highlighter singleton for the markdown pipeline (v4, JS regex engine).
 *
 * Why the JS engine: no WASM asset to bundle or fetch, synchronous creation,
 * pure JS (the vitest suites can really highlight). Why the async core: v4's
 * sync constructor cannot load languages after construction (its loadLanguage
 * is internally async) — the pipeline is async anyway, so every lang resolves
 * through an awaited `loadLanguage` with an explicit loader map (Vite cannot
 * statically analyze a templated `import('shiki/langs/…')`).
 *
 * Dual theme: `codeToHtml(..., themes: { light, dark }, defaultColor: false)`
 * emits `--shiki-light/--shiki-dark` custom properties; the page CSS switches
 * them via `html.dark` — a color-mode change never re-highlights.
 *
 * Results are cached per (lang, code) and fail-open: any problem answers null
 * and the caller keeps the plain block.
 *
 * @module composables/use-shiki
 */

import { createHighlighterCore, type HighlighterCore, type LanguageInput } from 'shiki/core';
import { createJavaScriptRegexEngine } from 'shiki/engine/javascript';
import { logUtil } from '~/utils/log';
import githubLight from 'shiki/themes/github-light.mjs';
import githubDark from 'shiki/themes/github-dark.mjs';

/** Blocks above this size stay plain text (highlight cost + DOM weight). */
export const MAX_HIGHLIGHT_CHARS = 100 * 1024;
/** Line-count companion to the char cap (a wall of 1-char lines). */
export const MAX_HIGHLIGHT_LINES = 2000;

type Highlighter = HighlighterCore;
type LangLoader = () => Promise<{ default: LanguageInput }>;

/** Explicit loaders — an enumerated map keeps Vite's code-splitting exact. */
const LANG_LOADERS: Record<string, LangLoader> = {
  typescript: () => import('shiki/langs/typescript.mjs'),
  javascript: () => import('shiki/langs/javascript.mjs'),
  json: () => import('shiki/langs/json.mjs'),
  python: () => import('shiki/langs/python.mjs'),
  html: () => import('shiki/langs/html.mjs'),
  css: () => import('shiki/langs/css.mjs'),
  shell: () => import('shiki/langs/shellscript.mjs'),
  markdown: () => import('shiki/langs/markdown.mjs'),
  yaml: () => import('shiki/langs/yaml.mjs'),
  sql: () => import('shiki/langs/sql.mjs'),
  go: () => import('shiki/langs/go.mjs'),
  rust: () => import('shiki/langs/rust.mjs'),
  java: () => import('shiki/langs/java.mjs'),
  c: () => import('shiki/langs/c.mjs'),
  cpp: () => import('shiki/langs/cpp.mjs'),
  csharp: () => import('shiki/langs/csharp.mjs'),
  xml: () => import('shiki/langs/xml.mjs'),
  diff: () => import('shiki/langs/diff.mjs'),
  ini: () => import('shiki/langs/ini.mjs'),
  toml: () => import('shiki/langs/toml.mjs'),
  kotlin: () => import('shiki/langs/kotlin.mjs'),
  php: () => import('shiki/langs/php.mjs'),
  ruby: () => import('shiki/langs/ruby.mjs')
};

/** Fence language → the grammar id in LANG_LOADERS. */
const LANG_ALIASES: Record<string, string> = {
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  py: 'python',
  python3: 'python',
  sh: 'shell',
  bash: 'shell',
  zsh: 'shell',
  console: 'shell',
  yml: 'yaml',
  golang: 'go',
  'c++': 'cpp',
  cc: 'cpp',
  hpp: 'cpp',
  cs: 'csharp',
  kt: 'kotlin',
  rb: 'ruby',
  htm: 'html',
  dockerfile: 'shell'
};

let highlighterPromise: Promise<Highlighter> | null = null;
const loadedLangs = new Set<string>();
const cache = new Map<string, string>();

async function getHighlighter(): Promise<Highlighter> {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighterCore({
      themes: [githubLight, githubDark],
      langs: [],
      engine: createJavaScriptRegexEngine({ forgiving: true })
    });
  }
  return highlighterPromise;
}

/**
 * Resolve a fence language to a loadable grammar id, or null when the
 * language is unknown (the caller keeps the plain block).
 * @param lang The raw fence info string.
 */
export function resolveShikiLang(lang: string): string | null {
  const key = lang.trim().toLowerCase();
  const id = key ? (LANG_ALIASES[key] ?? key) : '';
  return id && LANG_LOADERS[id] ? id : null;
}

/**
 * Highlight one code block with the dual (light/dark) theme.
 *
 * @param code The raw code text.
 * @param lang The fence language (aliased internally; unknown → null).
 * @returns Sanitizer-ready HTML (`<pre class="shiki">…`), or null when the
 *   block is too large, the language is unknown, or anything threw.
 */
export async function highlightCode(code: string, lang: string): Promise<string | null> {
  try {
    if (!code || code.length > MAX_HIGHLIGHT_CHARS) return null;
    if (code.split('\n').length > MAX_HIGHLIGHT_LINES) return null;
    const resolved = resolveShikiLang(lang);
    if (!resolved) return null;

    const hl = await getHighlighter();
    if (!loadedLangs.has(resolved)) {
      const loader = LANG_LOADERS[resolved];
      if (!loader) return null;
      await hl.loadLanguage((await loader()).default);
      loadedLangs.add(resolved);
    }

    const key = `${resolved}\u0000${code}`;
    const cached = cache.get(key);
    if (cached) return cached;

    const html = hl.codeToHtml(code, {
      lang: resolved,
      themes: { light: 'github-light', dark: 'github-dark' },
      defaultColor: false
    });
    cache.set(key, html);
    return html;
  } catch (error) {
    logUtil.w('[use-shiki] highlight failed:', error);
    return null;
  }
}

/** Test seam: drop the singleton, loaded languages and cache. */
export function _resetShikiForTest(): void {
  highlighterPromise = null;
  loadedLangs.clear();
  cache.clear();
}
