/**
 * The persona preset catalogue: the built-in entries plus the helpers the three
 * surfaces share — the 预设 panel, the mandatory new-session dialog and the
 * top-bar preset viewer.
 *
 * A preset is a named payload of persona files + the character block (both role
 * names and avatars). Built-ins are VIRTUAL: their content is composed from the
 * language templates at use time, they are never Dexie rows, and therefore can
 * be neither renamed nor deleted.
 */
import type { AgentConfig, PersonaPreset, PresetCharacter } from '@/composables/db';
// Stable module specifiers so tests can vi.mock the bridge / logger.
/* eslint-disable @typescript-eslint/no-restricted-imports */
import { setAgentConfig } from '~/composables/bridge/agent-config';
import { logUtil } from '~/utils/log';
/* eslint-enable @typescript-eslint/no-restricted-imports */

/** Built-in entry ids (the virtual, non-deletable presets). */
export type BuiltinPresetId = 'sherry' | 'coding';

/** Translate function shape the catalogue needs (a component's `t`). */
export type TranslateFn = (key: string, params?: Record<string, unknown>) => string;

/** i18n keys of the ROLE.md statement lines (central locales, four languages). */
export const ROLE_LINE_KEYS = { ai: 'config.role.aiLine', user: 'config.role.userLine' } as const;

/** One entry of the catalogue (a built-in entry or a saved preset). */
export interface PresetEntry {
  /** Stable id: the built-in id, or `user:<dexie id>` for a saved preset. */
  id: string;
  /** True for the virtual built-ins (no delete / rename possible). */
  builtin: boolean;
  /** i18n key of the display name (built-ins only — their name is localized). */
  nameKey?: string;
  /** i18n key of the badge (built-ins only). */
  badgeKey?: string;
  /** The saved preset row (user entries only). */
  userPreset?: PersonaPreset;
}

/**
 * The built-in entries, in display order: 编程助手 FIRST — it is the default
 * preset (a new session starts as a plain coding assistant), 橘雪莉 stays as
 * the role-play built-in behind it.
 */
export const BUILTIN_PRESETS: ReadonlyArray<{
  id: BuiltinPresetId;
  nameKey: string;
  badgeKey: string;
}> = [
  {
    id: 'coding',
    nameKey: 'config.persona.preset.builtinCodingName',
    badgeKey: 'config.persona.preset.defaultBadge'
  },
  {
    id: 'sherry',
    nameKey: 'config.persona.preset.defaultName',
    badgeKey: 'config.persona.preset.builtinBadge'
  }
];

/** The preset a session gets when nothing else is chosen. */
export const DEFAULT_PRESET_ID: BuiltinPresetId = 'coding';

/**
 * The preset id a saved preset binds as.
 * @param presetId Dexie row id of the preset.
 * @returns The catalogue id (`user:<id>`).
 */
export const userPresetEntryId = (presetId: number): string => `user:${presetId}`;

/**
 * Built-ins first (in display order), then the saved presets (creation order).
 * @param userPresets The saved presets from the shared `usePersonaPresets` singleton.
 * @returns The catalogue in display order.
 */
export function catalogEntries(userPresets: PersonaPreset[]): PresetEntry[] {
  const builtins: PresetEntry[] = BUILTIN_PRESETS.map(builtin => ({
    id: builtin.id,
    builtin: true,
    nameKey: builtin.nameKey,
    badgeKey: builtin.badgeKey
  }));
  const users: PresetEntry[] = userPresets.map(preset => ({
    id: userPresetEntryId(preset.id ?? -1),
    builtin: false,
    userPreset: preset
  }));
  return [...builtins, ...users];
}

/**
 * A preset's display name.
 * @param entry Catalogue entry.
 * @param t The component's translate function (built-in names are localized).
 * @returns The name to render.
 */
export function entryName(entry: PresetEntry, t: TranslateFn): string {
  if (entry.builtin) return t(entry.nameKey ?? '');
  return entry.userPreset?.name ?? '';
}

/**
 * ROLE.md content composed from a character block in the caller's UI language
 * (the templates in `workspace/template/<lang>/ROLE.md` carry the same shape).
 *
 * A blank name contributes NO line, and both blank yields an empty file — the
 * prompt then gains no ROLE block at all (the 编程助手 built-in relies on this).
 * @param character Role names to state.
 * @param t The component's translate function.
 * @returns The ROLE.md text ('' when neither role is named).
 */
export function composeRoleFile(character: PresetCharacter, t: TranslateFn): string {
  const lines: string[] = [];
  const aiName = character.aiName.trim();
  const userName = character.userName.trim();
  if (aiName) lines.push(t(ROLE_LINE_KEYS.ai, { name: aiName }));
  if (userName) lines.push(t(ROLE_LINE_KEYS.user, { name: userName }));
  return lines.length > 0 ? `# ROLE.md\n\n${lines.join('\n')}\n` : '';
}

/** Payload every apply path writes: the persona files + the character + the agent config. */
export interface PersonaPresetPayload {
  /** File basename → content (AGENTS.md / SOUL.md / USER.md / ROLE.md). */
  content: Record<string, string>;
  /** Role names + avatars (written to the Dexie global profile). */
  character: PresetCharacter;
  /**
   * The 工具 / 中间件 / 子代理模型 selection (written to the session's agent config
   * when the apply path knows its session). `{}` = every default: all tools, all
   * switches on, every role on its `model_tier`.
   */
  agent?: AgentConfig;
}

/**
 * The payload of a built-in entry, composed from the language template.
 *
 * - 编程助手: the operating rules only — soul and user profile EMPTY, and BOTH
 *   role names empty (no role statement: the prompt gains no ROLE block).
 * - 橘雪莉: the full template plus the shipped default names.
 * @param id Built-in entry id.
 * @param template The language template (`readSystemPromptTemplate`), already fetched.
 * @param t The component's translate function.
 * @returns The apply payload (ROLE.md composed from the character).
 */
export function builtinPayload(
  id: BuiltinPresetId,
  template: Record<string, string>,
  t: TranslateFn
): PersonaPresetPayload {
  const character: PresetCharacter =
    id === 'coding'
      ? {
          aiName: '',
          // Empty avatars: 编程助手 names nobody and ships no avatar — both roles
          // render the neutral gray placeholder (DEFAULT_PLACEHOLDER_AVATAR).
          aiAvatar: '',
          userName: '',
          userAvatar: ''
        }
      : { ...DEFAULT_CACHED_CHARACTER };
  const content: Record<string, string> =
    id === 'coding'
      ? { 'AGENTS.md': template['AGENTS.md'] ?? '', 'SOUL.md': '', 'USER.md': '' }
      : {
          'AGENTS.md': template['AGENTS.md'] ?? '',
          'SOUL.md': template['SOUL.md'] ?? '',
          'USER.md': template['USER.md'] ?? ''
        };
  content['ROLE.md'] = composeRoleFile(character, t);
  // Neither built-in pins a tool / middleware / model choice: `{}` means every
  // default, so a built-in keeps behaving exactly like a session without a config.
  return { content, character, agent: {} };
}

/**
 * Resolve a catalogue entry to its apply-ready payload.
 * @param entry Catalogue entry.
 * @param locale Current UI locale (selects the language template for built-ins).
 * @param t The component's translate function.
 * @returns The payload (persona files incl. ROLE.md + the character block).
 */
export async function loadPresetPayload(
  entry: PresetEntry,
  locale: string,
  t: TranslateFn
): Promise<PersonaPresetPayload> {
  if (entry.builtin) {
    const template = await readSystemPromptTemplate(locale);
    return builtinPayload(entry.id as BuiltinPresetId, template, t);
  }
  const preset = entry.userPreset;
  if (!preset) throw new Error(`[persona-catalog] unknown preset entry: ${entry.id}`);
  const stored = preset.content ?? {};
  const character: PresetCharacter = preset.character ?? { ...DEFAULT_CACHED_CHARACTER };
  return {
    agent: preset.agent ?? {},
    content: {
      'AGENTS.md': stored['AGENTS.md'] ?? '',
      'SOUL.md': stored['SOUL.md'] ?? '',
      'USER.md': stored['USER.md'] ?? '',
      'ROLE.md': composeRoleFile(character, t)
    },
    character
  };
}

/**
 * Write a preset payload: the persona files through the API (read back and
 * verified), the character into the Dexie global profile new sessions copy, and
 * — when the caller names the session — the agent config (工具 / 中间件 /
 * 子代理模型) into that session's own registers.
 * @param payload Payload to apply.
 * @param sessionId Session to write the agent config for; omitted = files and
 *   character only (no session exists yet).
 * @throws When the file write cannot be verified (the caller surfaces the failure).
 */
export async function applyPresetPayload(payload: PersonaPresetPayload, sessionId?: string): Promise<void> {
  await writeSystemPrompt(payload.content);
  const written = await readSystemPrompt();
  const verified = !!written && Object.entries(payload.content).every(([file, content]) => written[file] === content);
  if (!verified) throw new Error('[persona-catalog] applied content verification failed');
  await cacheCharacter({
    session_id: GLOBAL_SESSION_KEY,
    userName: payload.character.userName,
    userAvatar: payload.character.userAvatar,
    aiName: payload.character.aiName,
    aiAvatar: payload.character.aiAvatar
  });
  if (sessionId) {
    // A failure here must not undo the persona write (the session then runs
    // with every default, which is exactly a preset without an agent block).
    try {
      await setAgentConfig(sessionId, payload.agent ?? {});
    } catch (e) {
      logUtil.e('[persona-catalog] agent config write failed:', e);
    }
  }
}
