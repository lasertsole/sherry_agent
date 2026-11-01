/**
 * The persona preset catalogue: the built-in entries plus the helpers the three
 * surfaces share — the preset panel, the mandatory new-session dialog and the
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
export type BuiltinPresetId = 'pure' | 'companion' | 'sherry';

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
 * - Pure (`pure`): nobody named, no persona content at all, only the catalogue's
 *   REQUIRED tools and every optional middleware off — the bare floor a session
 *   can run on;
 * - Companion (`companion`): the full role-play persona with the orchestration
 *   surfaces switched off (no Tasks & planning / subagent tools, no optional middleware);
 * - Full (`sherry`): the DEFAULT — the full persona with every tool and switch on.
 *
 * The `sherry` ID is kept as-is: sessions already bound to this built-in store
 * it in their current-session preset binding, and only the LABEL became Full (the shipped
 * character is still 橘雪莉 (Sherry) — the preset names the configuration, not the role).
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

/** The preset a session gets when nothing else is chosen (Full, the full setup). */
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
 * prompt then gains no ROLE block at all (the Pure built-in relies on this).
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

/** Work-flow skills Companion excludes from its index (the coding chain). */
export const COMPANION_EXCLUDED_SKILLS: readonly string[] = [
  'bugfix-contract',
  'code-wiki',
  'taskflow',
  'ulw-execute',
  'todolist'
];

/** Tool groups Companion keeps OFF entirely: the orchestration surfaces. */
export const COMPANION_DISABLED_TOOL_GROUPS: readonly string[] = ['tasks', 'subagents'];

/**
 * The Operating Instructions sections Companion drops, per UI language: the task-orchestration
 * doctrine (the literal headings 编排者信条 / 何时创建 Todo / 委派 / 转换屏障 / 完成契约 / 反模式) and
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
 * The SOUL.md line Companion drops, per UI language: the couple relationship
 * with 远野汉娜 that the shipped templates carry.
 *
 * The companion preset keeps the persona's warmth, curiosity and tone; only the
 * romantic pairing goes, because a general companion setting should not colour
 * every interaction with it.
 *
 * Matched by the literal bold marker the shipped templates use
 * (`workspace/template/<lang>/SOUL.md`); `persona-catalog.test.ts` reads those
 * files and fails when a reworded template stops matching.
 */
export const COMPANION_DROPPED_RELATIONSHIP_MARKERS: Record<string, string> = {
  zh: '**恋爱关系**',
  en: '**couple relationship**',
  ja: '**恋人関係**',
  ko: '**연인 관계**'
};

/**
 * Strip the relationship line out of one SOUL template (see
 * {@link COMPANION_DROPPED_RELATIONSHIP_MARKERS}).
 *
 * The whole line carrying the marker goes (a Markdown bullet is one line in
 * every shipped template); unknown languages and templates without a match come
 * back untouched.
 * @param text The SOUL.md template.
 * @param locale UI locale selecting the marker.
 * @returns The text without the relationship line.
 */
export function stripRelationshipLines(text: string, locale: string): string {
  const marker = COMPANION_DROPPED_RELATIONSHIP_MARKERS[locale.split('-')[0] ?? ''];
  if (!marker) return text;
  return text
    .split('\n')
    .filter(line => !line.includes(marker))
    .join('\n');
}

/**
 * Strip the task / orchestration content out of one Operating Instructions template (see
 * {@link COMPANION_DROPPED_SECTIONS}).
 *
 * A dropped SECTION runs from its heading to the next heading of the same or a
 * higher level (the shipped templates end with it). A dropped BULLET is one
 * ``- **label**`` line anywhere; a section left without any content is dropped
 * as well, so the empty husk of the operating rules does not survive. Unknown languages and
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
 * - Pure: only the catalogue's required tools and skills — the locked set the
 *   service would refuse to drop anyway, so the preset states the floor
 *   explicitly — and every optional middleware off;
 * - Companion: every tool EXCEPT the Tasks & planning / subagent groups, every
 *   optional middleware off, and every NON-required skill except the coding
 *   work-flow set;
 * - Full: no opinion (every tool, every switch on, every skill in the index, and
 *   the Summarization nudge left ON).
 *
 * The compression nudge (`middleware_options.Summarization.nudge`) is pinned OFF
 * by both restricted presets and left at its default by Full — it is the
 * full-setup preset that keeps it.
 *
 * A restriction that cannot be computed (the catalogue never loaded) THROWS: a
 * silent fallback to `{}` would apply the opposite of what the preset promises.
 * @param id Built-in entry id.
 * @param facts The loaded catalogue.
 * @returns The agent block for the session config.
 */
export function builtinAgentConfig(id: BuiltinPresetId, facts: PresetCatalogFacts): AgentConfig {
  if (id !== 'pure' && id !== 'companion') return {};
  if (facts.tools.length === 0 || facts.skills.length === 0) {
    throw new Error('[persona-catalog] the tool catalogue is unavailable; cannot compose the preset');
  }
  const block: AgentConfig = {};
  block.tools =
    id === 'pure'
      ? facts.tools.filter(tool => tool.required === true).map(tool => tool.name)
      : facts.tools.filter(tool => !COMPANION_DISABLED_TOOL_GROUPS.includes(tool.group)).map(tool => tool.name);
  block.skills = builtinSkills(id, facts.skills).map(skill => skill.name);
  if (facts.gateableMiddlewares.length > 0) {
    block.middlewares_disabled = [...facts.gateableMiddlewares];
  }
  // Only Full leaves the compression nudge on (its absence = the default).
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
    // Pure keeps the locked floor only; Full has no opinion (every skill).
    return id === 'pure' ? required : skills;
  }
  const excluded = new Set(COMPANION_EXCLUDED_SKILLS);
  return [...required, ...optional.filter(skill => !excluded.has(skill.name))];
}

/** Payload every apply path writes: the persona files + the character + the agent config. */
export interface PersonaPresetPayload {
  /** File basename → content (AGENTS.md / SOUL.md / USER.md / ROLE.md). */
  content: Record<string, string>;
  /** Role names + avatars (written to the Dexie global profile). */
  character: PresetCharacter;
  /**
   * The Tools / Middlewares / Agent models / Skills selection (written to the session's agent
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
 * - Pure: EVERY persona file empty (no operating rules either) and nobody named
 *   — the clean slate — plus an agent block that keeps only the required tools
 *   and turns every optional middleware off;
 * - Companion: the full template with the task-orchestration content stripped
 *   from the Operating Instructions (a companion is not an orchestrator) and the couple-relationship
 *   line stripped from SOUL, plus the default names;
 * - Full: the full template plus the shipped default names.
 * @param id Built-in entry id.
 * @param template The language template (`readSystemPromptTemplate`), already fetched.
 * @param t The component's translate function.
 * @param facts The loaded catalogue (drives Pure / Companion's agent block).
 * @param locale UI locale (selects Companion's section-strip list).
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
        // Pure names nobody — both roles render the neutral gray placeholder
        // (DEFAULT_PLACEHOLDER_AVATAR) instead of an avatar.
        aiName: '',
        aiAvatar: '',
        userName: '',
        userAvatar: ''
      };
  const rules = template['AGENTS.md'] ?? '';
  let content: Record<string, string>;
  if (id === 'pure') {
    content = { 'AGENTS.md': '', 'SOUL.md': '', 'USER.md': '' };
  } else {
    content = {
      'AGENTS.md': id === 'companion' ? stripTaskSections(rules, locale) : rules,
      'SOUL.md':
        id === 'companion' ? stripRelationshipLines(template['SOUL.md'] ?? '', locale) : (template['SOUL.md'] ?? ''),
      'USER.md': template['USER.md'] ?? ''
    };
  }
  content['ROLE.md'] = composeRoleFile(character, t);
  // No built-in pins a model: `null` = follow the environment config, which is
  // also what the panel's Main agent sub-tab starts at.
  return { content, character, agent: builtinAgentConfig(id, facts), mainModel: null };
}

/**
 * Everything `createSessionFromPreset` needs from its caller.
 *
 * Injected rather than imported so this module stays a pure catalogue: the
 * character snapshot lives on the session page (`ensureSessionCharacter`), and
 * the navigation helpers come from the component's Nuxt context.
 */
export interface CreateSessionOptions {
  /** Current UI locale (selects the language template for built-ins). */
  locale: string;
  /** Translate function (component `t`). */
  t: TranslateFn;
  /** The loaded tool/middleware/skill catalogue. */
  facts: PresetCatalogFacts;
  /** Router (Nuxt `useRouter()`). */
  router: { push: (to: string) => Promise<unknown> };
  /** Locale-aware path builder (Nuxt `useLocalePath()`). */
  localePath: (path: string) => string;
  /** Lock the session's character snapshot (the page's helper). */
  ensureCharacter: (sessionId: string) => Promise<unknown>;
}

/**
 * Create a session from one preset: apply the persona files + agent block, bind
 * the preset, snapshot the character and announce the new row.
 *
 * The apply runs FIRST: a session locks its persona into the prompt snapshot at
 * the first build, so the files must already hold the preset when the session
 * starts. A failed apply throws and the caller keeps its dialog open — a
 * session created without its preset would silently run the previous persona.
 *
 * Extracted from the new-session dialog's `confirm()` so the onboarding flow
 * can create the session programmatically (one implementation, no duplication).
 * @param entry Catalogue entry to apply.
 * @param options Injected context (see {@link CreateSessionOptions}).
 * @returns The new session id.
 */
export async function createSessionFromPreset(entry: PresetEntry, options: CreateSessionOptions): Promise<string> {
  const payload = await loadPresetPayload(entry, options.locale, options.t, options.facts);
  // The id is minted first so the apply can also write the preset's agent
  // config (Tools / Middlewares / Agent models) into THIS session's registers.
  const sessionId = crypto.randomUUID();
  await applyPresetPayload(payload, sessionId);

  try {
    await cacheSessionPreset({
      session_id: sessionId,
      preset_id: entry.id,
      preset_name: entryName(entry, options.t)
    });
  } catch (e) {
    // The local preset binding is display metadata; a Dexie failure must not
    // prevent the session itself from being created.
    logUtil.e('[persona-catalog] preset binding cache failed:', e);
  }
  await options.ensureCharacter(sessionId);

  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const createTime = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
  const meta = { id: sessionId, title: options.t('history.newSession'), createTime, updatedAt: Date.now() };
  void cacheSessionMeta(meta).catch(e => {
    logUtil.e('[persona-catalog] session meta cache failed:', e);
  });
  // The left sidebar keeps its own in-memory list; tell it about the new row so
  // the session appears there without a reload.
  emit('session:created', meta);

  // `localePath` (not the named route): the i18n module's localized route
  // variants make a raw named push redirect, which vue-router surfaces as a
  // rejected navigation — the sidebar's own create path does the same.
  await options.router.push(options.localePath(`/home/${sessionId}`));
  return sessionId;
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
    // A preset saved before the Main agent sub-tab existed has no choice: null = the
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
 * — when the caller names the session — the agent config (Tools / Middlewares /
 * Agent models) into that session's own registers.
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
  try {
    await cacheCharacter({
      session_id: GLOBAL_SESSION_KEY,
      userName: payload.character.userName,
      userAvatar: payload.character.userAvatar,
      aiName: payload.character.aiName,
      aiAvatar: payload.character.aiAvatar
    });
  } catch (e) {
    // A local display cache must never block creating the session.
    logUtil.e('[persona-catalog] character cache write failed:', e);
  }
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
