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
import type { SessionModelProfile } from '~/composables/bridge/session';
// Stable module specifiers so tests can vi.mock the bridge / logger.
/* eslint-disable @typescript-eslint/no-restricted-imports */
import { setAgentConfig } from '~/composables/bridge/agent-config';
import { setSessionModel } from '~/composables/bridge/session';
import { logUtil } from '~/utils/log';
/* eslint-enable @typescript-eslint/no-restricted-imports */

/** Built-in entry ids (the virtual, non-deletable presets). */
export type BuiltinPresetId = 'pure' | 'coding' | 'companion' | 'sherry';

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
 * The built-in entries, in display order — the whole spectrum, read top to
 * bottom:
 *
 * - 纯净 (`pure`): nobody named, no persona content at all, only the catalogue's
 *   REQUIRED tools and every optional middleware off — the bare floor a session
 *   can run on;
 * - 编程助手 (`coding`): the operating-rules template, empty soul / user profile,
 *   no roles, every tool on;
 * - 情感陪伴 (`companion`): the full role-play persona with the orchestration
 *   surfaces switched off (no 任务与计划 / 子代理 tools, no optional middleware);
 * - 全量 (`sherry`): the DEFAULT — the full persona with every tool and switch on.
 *
 * The `sherry` ID is kept as-is: sessions already bound to this built-in store
 * it in their 当前会话预设 binding, and only the LABEL became 全量 (the shipped
 * character is still 橘雪莉 — the preset names the configuration, not the role).
 */
export const BUILTIN_PRESETS: ReadonlyArray<{
  id: BuiltinPresetId;
  nameKey: string;
  badgeKey: string;
}> = [
  {
    id: 'pure',
    nameKey: 'config.persona.preset.builtinPureName',
    badgeKey: 'config.persona.preset.builtinBadge'
  },
  {
    id: 'coding',
    nameKey: 'config.persona.preset.builtinCodingName',
    badgeKey: 'config.persona.preset.builtinBadge'
  },
  {
    id: 'companion',
    nameKey: 'config.persona.preset.builtinCompanionName',
    badgeKey: 'config.persona.preset.builtinBadge'
  },
  {
    id: 'sherry',
    nameKey: 'config.persona.preset.defaultName',
    badgeKey: 'config.persona.preset.defaultBadge'
  }
];

/** The preset a session gets when nothing else is chosen (the full 全量 setup). */
export const DEFAULT_PRESET_ID: BuiltinPresetId = 'sherry';

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

/**
 * The catalogue facts a built-in preset's agent block is computed from — the
 * same `GET /agent/catalog` response the three tabs render, so the client never
 * hardcodes tool names (a tool added to the backend lands in these presets too).
 */
export interface PresetCatalogFacts {
  /** Every main-agent tool: name, group id, and the lock flag. */
  tools: ReadonlyArray<{ name: string; group: string; required?: boolean }>;
  /** The gateable (switchable) middleware names. */
  gateableMiddlewares: readonly string[];
  /** Every skill the index can contain, with its lock flag (required first). */
  skills: ReadonlyArray<{ name: string; required?: boolean }>;
}

/** Tools 编程助手 turns off on top of its own selection (the memory store). */
export const CODING_DISABLED_TOOLS: readonly string[] = ['memory'];

/** Skills 编程助手 keeps besides the required chain (the work flow tools). */
export const CODING_SKILLS: readonly string[] = ['code-wiki', 'taskflow', 'ulw-execute', 'todolist'];

/** Tool groups 情感陪伴 keeps OFF entirely: the orchestration surfaces. */
export const COMPANION_DISABLED_TOOL_GROUPS: readonly string[] = ['tasks', 'subagents'];

/**
 * The 运行守则 sections 情感陪伴 drops, per UI language: the task-orchestration
 * doctrine (编排者信条 / 何时创建 Todo / 委派 / 转换屏障 / 完成契约 / 反模式) and
 * the two task bullets of the first section. A companion preset is not an
 * orchestrator, so the text that would steer it into planning-and-delegating is
 * removed rather than left to fight the preset's tool set.
 *
 * Matched by the literal heading/label text the shipped templates use
 * (`workspace/template/<lang>/AGENTS.md`); `persona-catalog.test.ts` reads those
 * files and fails when a reworded template stops matching.
 */
export const COMPANION_DROPPED_SECTIONS: Record<string, { sections: string[]; bullets: string[] }> = {
  zh: { sections: ['任务管理与编排'], bullets: ['任务分配', '任务执行'] },
  en: { sections: ['Task Management & Orchestration'], bullets: ['Task assignment', 'Task execution'] },
  ja: { sections: ['タスク管理とオーケストレーション'], bullets: ['タスクの割り当て', 'タスクの実行'] },
  ko: { sections: ['작업 관리 및 오케스트레이션'], bullets: ['작업 분배', '작업 실행'] }
};

/**
 * Strip the task / orchestration content out of one 运行守则 template (see
 * {@link COMPANION_DROPPED_SECTIONS}).
 *
 * A dropped SECTION runs from its heading to the next heading of the same or a
 * higher level (the shipped templates end with it). A dropped BULLET is one
 * ``- **label**`` line anywhere; a section left without any content is dropped
 * as well, so the empty husk of 运行守则 does not survive. Unknown languages and
 * templates without a match come back untouched.
 * @param text The AGENTS.md template.
 * @param locale UI locale selecting the match list.
 * @returns The text without the task-orchestration content.
 */
export function stripTaskSections(text: string, locale: string): string {
  const rules = COMPANION_DROPPED_SECTIONS[locale.split('-')[0] ?? ''];
  if (!rules) return text;
  const lines = text.split('\n');
  const kept: string[] = [];
  let dropping: number | null = null; // heading level currently being dropped
  for (const line of lines) {
    const heading = /^(#+)\s+(.*)$/.exec(line);
    if (heading) {
      const level = heading[1]!.length;
      if (dropping !== null && level <= dropping) dropping = null;
      if (dropping === null && rules.sections.some(title => heading[2]!.startsWith(title))) {
        dropping = level;
        continue;
      }
    }
    if (dropping !== null) continue;
    const bullet = /^-\s+\*\*(.+?)\*\*/.exec(line);
    if (bullet && rules.bullets.some(label => bullet[1]!.startsWith(label))) continue;
    kept.push(line);
  }
  // A section whose bullets all went away leaves a bare heading behind: drop
  // those second-level headings too (the file title stays regardless).
  const out: string[] = [];
  for (let index = 0; index < kept.length; index += 1) {
    const line = kept[index]!;
    const heading = /^(#+)\s+/.exec(line);
    if (heading && heading[1]!.length > 1) {
      const rest = kept.slice(index + 1).filter(candidate => candidate.trim() !== '');
      if (rest.length === 0 || /^#+\s+/.test(rest[0]!)) continue;
    }
    out.push(line);
  }
  return `${out
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()}\n`;
}

/**
 * The catalogue facts of a loaded agent-config store.
 * @param store The store (usually `useAgentConfigStore()`).
 * @param store.catalog
 * @param store.catalog.tools
 * @param store.catalog.skills
 * @param store.middlewares
 * @param store.middlewares.gateable
 * @returns The facts (empty lists until the catalogue has loaded).
 */
export function presetCatalogFacts(store: {
  catalog: { tools: PresetCatalogFacts['tools']; skills: PresetCatalogFacts['skills'] };
  middlewares: { gateable: ReadonlyArray<{ name: string }> };
}): PresetCatalogFacts {
  return {
    tools: store.catalog.tools,
    gateableMiddlewares: store.middlewares.gateable.map(entry => entry.name),
    skills: store.catalog.skills
  };
}

/**
 * The agent block a built-in pins ({} = every default).
 *
 * - 纯净: only the catalogue's required tools and skills — the locked set the
 *   service would refuse to drop anyway, so the preset states the floor
 *   explicitly — and every optional middleware off;
 * - 编程助手: every tool except the memory store, the required skills plus the
 *   four work-flow skills (`CODING_SKILLS`), and every optional middleware on;
 * - 情感陪伴: every tool EXCEPT the 任务与计划 / 子代理 groups, every optional
 *   middleware off, and every NON-required skill except 编程助手's four (the two
 *   presets split the index between them);
 * - 全量: no opinion (every tool, every switch on, every skill in the index, and
 *   the Summarization nudge left ON).
 *
 * The compression nudge (`middleware_options.Summarization.nudge`) is pinned OFF
 * by the three restricted presets and left at its default by 全量 — it is the
 * full-setup preset that keeps it.
 *
 * A restriction that cannot be computed (the catalogue never loaded) THROWS: a
 * silent fallback to `{}` would apply the opposite of what the preset promises.
 * @param id Built-in entry id.
 * @param facts The loaded catalogue.
 * @returns The agent block for the session config.
 */
export function builtinAgentConfig(id: BuiltinPresetId, facts: PresetCatalogFacts): AgentConfig {
  if (id !== 'pure' && id !== 'coding' && id !== 'companion') return {};
  if (facts.tools.length === 0 || facts.skills.length === 0) {
    throw new Error('[persona-catalog] the tool catalogue is unavailable; cannot compose the preset');
  }
  const block: AgentConfig = {};
  block.tools =
    id === 'pure'
      ? facts.tools.filter(tool => tool.required === true).map(tool => tool.name)
      : id === 'coding'
        ? facts.tools.filter(tool => !CODING_DISABLED_TOOLS.includes(tool.name)).map(tool => tool.name)
        : facts.tools.filter(tool => !COMPANION_DISABLED_TOOL_GROUPS.includes(tool.group)).map(tool => tool.name);
  block.skills = builtinSkills(id, facts.skills).map(skill => skill.name);
  if (id !== 'coding' && facts.gateableMiddlewares.length > 0) {
    block.middlewares_disabled = [...facts.gateableMiddlewares];
  }
  // Only 全量 leaves the compression nudge on (its absence = the default).
  block.middleware_options = { Summarization: { nudge: false } };
  return block;
}

/**
 * The skills a built-in keeps in the index (catalogue order, required always in).
 * @param id Built-in entry id.
 * @param skills The catalogue's skill list.
 * @returns The kept skills.
 */
function builtinSkills(id: BuiltinPresetId, skills: PresetCatalogFacts['skills']): PresetCatalogFacts['skills'] {
  const required = skills.filter(skill => skill.required === true);
  const optional = skills.filter(skill => skill.required !== true);
  if (id === 'pure' || id === 'sherry') {
    // 纯净 keeps the locked floor only; 全量 has no opinion (every skill).
    return id === 'pure' ? required : skills;
  }
  const codingPicks = new Set(CODING_SKILLS);
  const picked =
    id === 'coding'
      ? optional.filter(skill => codingPicks.has(skill.name))
      : optional.filter(skill => !codingPicks.has(skill.name));
  return [...required, ...picked];
}

/** Payload every apply path writes: the persona files + the character + the agent config. */
export interface PersonaPresetPayload {
  /** File basename → content (AGENTS.md / SOUL.md / USER.md / ROLE.md). */
  content: Record<string, string>;
  /** Role names + avatars (written to the Dexie global profile). */
  character: PresetCharacter;
  /**
   * The 工具 / 中间件 / 子代理模型 / 技能 selection (written to the session's agent
   * config when the apply path knows its session). `{}` = every default: all
   * tools, all switches on, every role on its `model_tier`, every skill in the
   * prompt's index.
   */
  agent?: AgentConfig;
  /**
   * The model the MAIN agent runs on in sessions from this preset: a profile
   * descriptor, or ``null`` to follow the environment config. `undefined` =
   * the preset has no opinion (an older saved preset) and the session keeps
   * whatever model it has.
   */
  mainModel?: SessionModelProfile | null;
}

/**
 * The payload of a built-in entry, composed from the language template.
 *
 * - 纯净: EVERY persona file empty (no operating rules either) and nobody named
 *   — the clean slate — plus an agent block that keeps only the required tools
 *   and turns every optional middleware off;
 * - 编程助手: the operating rules only — soul and user profile EMPTY, and BOTH
 *   role names empty (no role statement: the prompt gains no ROLE block);
 * - 情感陪伴: the full template with the task-orchestration content stripped
 *   from 运行守则 (a companion is not an orchestrator) plus the default names;
 * - 全量: the full template plus the shipped default names.
 * @param id Built-in entry id.
 * @param template The language template (`readSystemPromptTemplate`), already fetched.
 * @param t The component's translate function.
 * @param facts The loaded catalogue (drives 纯净 / 情感陪伴's agent block).
 * @param locale UI locale (selects 情感陪伴's section-strip list).
 * @returns The apply payload (ROLE.md composed from the character).
 */
export function builtinPayload(
  id: BuiltinPresetId,
  template: Record<string, string>,
  t: TranslateFn,
  facts: PresetCatalogFacts,
  locale: string
): PersonaPresetPayload {
  const named = id === 'companion' || id === 'sherry';
  const character: PresetCharacter = named
    ? { ...DEFAULT_CACHED_CHARACTER }
    : {
        // 纯净 / 编程助手 name nobody — both roles render the neutral gray
        // placeholder (DEFAULT_PLACEHOLDER_AVATAR) instead of an avatar.
        aiName: '',
        aiAvatar: '',
        userName: '',
        userAvatar: ''
      };
  const rules = template['AGENTS.md'] ?? '';
  let content: Record<string, string>;
  if (id === 'pure') {
    content = { 'AGENTS.md': '', 'SOUL.md': '', 'USER.md': '' };
  } else if (id === 'coding') {
    content = { 'AGENTS.md': rules, 'SOUL.md': '', 'USER.md': '' };
  } else {
    content = {
      'AGENTS.md': id === 'companion' ? stripTaskSections(rules, locale) : rules,
      'SOUL.md': template['SOUL.md'] ?? '',
      'USER.md': template['USER.md'] ?? ''
    };
  }
  content['ROLE.md'] = composeRoleFile(character, t);
  // No built-in pins a model: `null` = follow the environment config, which is
  // also what the panel's 主代理 sub-tab starts at.
  return { content, character, agent: builtinAgentConfig(id, facts), mainModel: null };
}

/**
 * Resolve a catalogue entry to its apply-ready payload.
 * @param entry Catalogue entry.
 * @param locale Current UI locale (selects the language template for built-ins).
 * @param t The component's translate function.
 * @param facts The loaded catalogue (built-ins' agent blocks are derived from it).
 * @returns The payload (persona files incl. ROLE.md + the character block).
 */
export async function loadPresetPayload(
  entry: PresetEntry,
  locale: string,
  t: TranslateFn,
  facts: PresetCatalogFacts
): Promise<PersonaPresetPayload> {
  if (entry.builtin) {
    const template = await readSystemPromptTemplate(locale);
    return builtinPayload(entry.id as BuiltinPresetId, template, t, facts, locale);
  }
  const preset = entry.userPreset;
  if (!preset) throw new Error(`[persona-catalog] unknown preset entry: ${entry.id}`);
  const stored = preset.content ?? {};
  const character: PresetCharacter = preset.character ?? { ...DEFAULT_CACHED_CHARACTER };
  return {
    agent: preset.agent ?? {},
    // A preset saved before the 主代理 sub-tab existed has no choice: null = the
    // environment config, which is what its draft displayed too.
    mainModel: preset.main_model ?? null,
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
    // The main model rides its own endpoint (the session-model control owns
    // that key); `undefined` = the preset has no opinion, so nothing is written.
    if (payload.mainModel !== undefined) {
      try {
        await setSessionModel(sessionId, payload.mainModel);
      } catch (e) {
        logUtil.e('[persona-catalog] main-model write failed:', e);
      }
    }
  }
}
