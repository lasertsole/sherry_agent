/**
 * The shared persona preset catalogue — the one source of the preset list, the
 * built-in payloads and the ROLE.md statement, used by the 预设 panel, the
 * mandatory new-session dialog and the top-bar preset viewer.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  BUILTIN_PRESETS,
  COMPANION_DISABLED_TOOL_GROUPS,
  DEFAULT_PRESET_ID,
  applyPresetPayload,
  builtinPayload,
  catalogEntries,
  composeRoleFile,
  entryName,
  loadPresetPayload,
  presetCatalogFacts,
  userPresetEntryId
} from '@/composables/persona-catalog';
import type { PersonaPreset } from '@/composables/db';
import type { PresetCatalogFacts } from '@/composables/persona-catalog';

const bridge = vi.hoisted(() => ({
  readSystemPrompt: vi.fn(),
  readSystemPromptTemplate: vi.fn(),
  writeSystemPrompt: vi.fn()
}));
vi.mock('~/composables/bridge', () => bridge);

const agentBridge = vi.hoisted(() => ({ setAgentConfig: vi.fn(async () => ({ config: {}, pending: false })) }));
vi.mock('~/composables/bridge/agent-config', () => agentBridge);

const db = vi.hoisted(() => ({
  DEFAULT_CACHED_CHARACTER: {
    aiName: '橘雪莉',
    aiAvatar: '/avatar/assistant.jpg',
    userName: '远野汉娜',
    userAvatar: '/avatar/user.jpg'
  },
  GLOBAL_SESSION_KEY: '__global__',
  cacheCharacter: vi.fn(async () => undefined)
}));
vi.mock('@/composables/db', () => db);

/**
 * The stub's translate function shape (keys resolve to themselves).
 * @param key
 * @param params
 */
const t = ((key: string, params?: Record<string, unknown>) => {
  const table: Record<string, string> = {
    'config.persona.preset.builtinPureName': '纯净',
    'config.persona.preset.builtinCodingName': '编程助手',
    'config.persona.preset.builtinCompanionName': '情感陪伴',
    'config.persona.preset.defaultName': '全量',
    'config.persona.preset.defaultBadge': '默认',
    'config.persona.preset.builtinBadge': '内置',
    'config.role.aiLine': '你将扮演{name}。',
    'config.role.userLine': '用户将扮演{name}。'
  };
  let text = table[key] ?? key;
  for (const [name, value] of Object.entries(params ?? {})) text = text.replaceAll(`{${name}}`, String(value));
  return text;
}) as (key: string, params?: Record<string, unknown>) => string;

const TEMPLATE = {
  'AGENTS.md': '# AGENTS.md\nrules',
  'SOUL.md': '# SOUL.md\nsoul',
  'USER.md': '# USER.md\nuser',
  'ROLE.md': 'ignored — composed'
};

/** The catalogue facts the built-in agent blocks derive from (a real subset). */
const FACTS = {
  tools: [
    { name: 'read_file', group: 'files', required: true },
    { name: 'terminal', group: 'terminal', required: true },
    { name: 'message_search', group: 'memory', required: true },
    { name: 'memory', group: 'memory' },
    { name: 'web_search', group: 'web' },
    { name: 'taskflow_create', group: 'tasks' },
    { name: 'todoread', group: 'tasks' },
    { name: 'sessions_spawn', group: 'subagents' }
  ],
  gateableMiddlewares: ['TodoContinuationEnforcer', 'TaskIntentMiddleware', 'SubagentCompletionDrainMiddleware']
};

/** Every built-in composes with the catalogue; these ids pin no agent block. */
const NO_AGENT: PresetCatalogFacts = { tools: [], gateableMiddlewares: [] };

const userPreset = (overrides: Partial<PersonaPreset> = {}): PersonaPreset => ({
  id: 7,
  name: '我的预设',
  content: { 'AGENTS.md': 'A', 'SOUL.md': 'S', 'USER.md': 'U' },
  createdAt: 0,
  updatedAt: 0,
  ...overrides
});

describe('persona catalogue', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('lists 纯净 / 编程助手 / 情感陪伴 / 全量, with 全量 the default', () => {
    expect(BUILTIN_PRESETS.map(entry => entry.id)).toEqual(['pure', 'coding', 'companion', 'sherry']);
    expect(DEFAULT_PRESET_ID).toBe('sherry');
    const entries = catalogEntries([]);
    expect(entries.map(entry => entry.id)).toEqual(['pure', 'coding', 'companion', 'sherry']);
    expect(entries.every(entry => entry.builtin)).toBe(true);
    expect(entries.map(entry => entryName(entry, t))).toEqual(['纯净', '编程助手', '情感陪伴', '全量']);
    // Only the default carries the 默认 badge; the other three read 内置.
    expect(entries.map(entry => t(entry.badgeKey ?? ''))).toEqual(['内置', '内置', '内置', '默认']);
  });

  it('appends the saved presets after the built-ins', () => {
    const entries = catalogEntries([userPreset(), userPreset({ id: 9, name: 'Second' })]);

    expect(entries.map(entry => entry.id)).toEqual([
      'pure',
      'coding',
      'companion',
      'sherry',
      userPresetEntryId(7),
      userPresetEntryId(9)
    ]);
    expect(entryName(entries[4]!, t)).toBe('我的预设');
  });

  it('composes the ROLE statement per name and drops blank ones', () => {
    const character = (aiName: string, userName: string) => ({
      aiName,
      aiAvatar: 'a',
      userName,
      userAvatar: 'u'
    });

    expect(composeRoleFile(character('小樱', '小明'), t)).toBe('# ROLE.md\n\n你将扮演小樱。\n用户将扮演小明。\n');
    expect(composeRoleFile(character('小樱', ''), t)).toBe('# ROLE.md\n\n你将扮演小樱。\n');
    // Both blank ⇒ no statement at all (the 编程助手 default).
    expect(composeRoleFile(character('', ''), t)).toBe('');
  });

  it('builds the 编程助手 payload with no roles and no soul / user profile', () => {
    const payload = builtinPayload('coding', TEMPLATE, t, NO_AGENT);

    expect(payload.content).toEqual({
      'AGENTS.md': TEMPLATE['AGENTS.md'],
      'SOUL.md': '',
      'USER.md': '',
      'ROLE.md': ''
    });
    expect(payload.character).toEqual({
      aiName: '',
      // No avatar of its own: both roles render the neutral placeholder.
      aiAvatar: '',
      userName: '',
      userAvatar: ''
    });
  });

  it('builds the 全量 payload from the full template and the default roles', () => {
    const payload = builtinPayload('sherry', TEMPLATE, t, FACTS);

    expect(payload.content['AGENTS.md']).toBe(TEMPLATE['AGENTS.md']);
    expect(payload.content['SOUL.md']).toBe(TEMPLATE['SOUL.md']);
    expect(payload.content['USER.md']).toBe(TEMPLATE['USER.md']);
    expect(payload.content['ROLE.md']).toBe('# ROLE.md\n\n你将扮演橘雪莉。\n用户将扮演远野汉娜。\n');
    expect(payload.character.aiName).toBe('橘雪莉');
    expect(payload.character.userName).toBe('远野汉娜');
    // 全量 pins nothing: every tool, every switch on.
    expect(payload.agent).toEqual({});
  });

  it('builds the 纯净 payload with no persona, the required tools and no switches', () => {
    const payload = builtinPayload('pure', TEMPLATE, t, FACTS);

    // Every file empty — including the operating rules — and nobody named.
    expect(payload.content).toEqual({ 'AGENTS.md': '', 'SOUL.md': '', 'USER.md': '', 'ROLE.md': '' });
    expect(payload.character).toEqual({ aiName: '', aiAvatar: '', userName: '', userAvatar: '' });
    // The catalogue's locked set is the whole tool list this preset enables…
    expect(payload.agent!.tools).toEqual(['read_file', 'terminal', 'message_search']);
    // …and every optional middleware is off too.
    expect(payload.agent!.middlewares_disabled).toEqual(FACTS.gateableMiddlewares);
  });

  it('builds the 情感陪伴 payload on the full persona with the orchestration off', () => {
    const payload = builtinPayload('companion', TEMPLATE, t, FACTS);

    expect(payload.content['SOUL.md']).toBe(TEMPLATE['SOUL.md']);
    expect(payload.character.aiName).toBe('橘雪莉');
    // Every tool EXCEPT the 任务与计划 / 子代理 groups keeps moving together.
    expect(payload.agent!.tools).toEqual(['read_file', 'terminal', 'message_search', 'memory', 'web_search']);
    expect(payload.agent!.middlewares_disabled).toEqual(FACTS.gateableMiddlewares);
    for (const name of ['taskflow_create', 'todoread', 'sessions_spawn']) {
      expect(payload.agent!.tools).not.toContain(name);
    }
    // The disabled groups are the orchestration surfaces.
    expect(COMPANION_DISABLED_TOOL_GROUPS).toEqual(['tasks', 'subagents']);
  });

  it('refuses to compose 纯净 / 情感陪伴 without the catalogue', () => {
    // A silent `{}` would apply the OPPOSITE of what these presets promise.
    expect(() => builtinPayload('pure', TEMPLATE, t, NO_AGENT)).toThrow('tool catalogue is unavailable');
    expect(() => builtinPayload('companion', TEMPLATE, t, NO_AGENT)).toThrow('tool catalogue is unavailable');
    // The two unopinionated built-ins never need it.
    expect(builtinPayload('coding', TEMPLATE, t, NO_AGENT).agent).toEqual({});
    expect(builtinPayload('sherry', TEMPLATE, t, NO_AGENT).agent).toEqual({});
  });

  it('reads the facts off an agent-config store', () => {
    const facts = presetCatalogFacts({
      catalog: { tools: FACTS.tools },
      middlewares: { gateable: [{ name: 'TaskIntentMiddleware' }] }
    });

    expect(facts.tools).toBe(FACTS.tools);
    expect(facts.gateableMiddlewares).toEqual(['TaskIntentMiddleware']);
  });

  it('loads a built-in payload from the language template and a saved preset from its row', async () => {
    bridge.readSystemPromptTemplate.mockResolvedValue(TEMPLATE);

    const builtin = await loadPresetPayload(catalogEntries([])[1]!, 'zh', t, NO_AGENT);
    expect(bridge.readSystemPromptTemplate).toHaveBeenCalledWith('zh');
    expect(builtin.content['AGENTS.md']).toBe(TEMPLATE['AGENTS.md']);

    const user = await loadPresetPayload(catalogEntries([userPreset()])[4]!, 'zh', t, NO_AGENT);
    expect(user.content).toEqual({
      'AGENTS.md': 'A',
      'SOUL.md': 'S',
      'USER.md': 'U',
      // No character block on the row: the defaults state the roles.
      'ROLE.md': '# ROLE.md\n\n你将扮演橘雪莉。\n用户将扮演远野汉娜。\n'
    });
  });

  it('applies a payload through the API (verified) and the global character profile', async () => {
    const written: Record<string, string> = {};
    bridge.writeSystemPrompt.mockImplementation(async (map: Record<string, string>) => {
      Object.assign(written, map);
    });
    bridge.readSystemPrompt.mockImplementation(async () => ({ ...written }));
    const payload = builtinPayload('coding', TEMPLATE, t, NO_AGENT);

    await applyPresetPayload(payload);

    expect(bridge.writeSystemPrompt).toHaveBeenCalledWith(payload.content);
    expect(db.cacheCharacter).toHaveBeenCalledWith({
      session_id: '__global__',
      aiName: '',
      aiAvatar: '',
      userName: '',
      userAvatar: ''
    });
  });

  it('pins an agent block only where a preset restricts something', () => {
    expect(builtinPayload('pure', TEMPLATE, t, FACTS).agent).not.toEqual({});
    expect(builtinPayload('companion', TEMPLATE, t, FACTS).agent).not.toEqual({});
    for (const id of ['coding', 'sherry'] as const) {
      expect(builtinPayload(id, TEMPLATE, t, FACTS).agent).toEqual({});
    }
  });

  it('writes the agent block to the session when the apply names one', async () => {
    const written: Record<string, string> = {};
    bridge.writeSystemPrompt.mockImplementation(async (map: Record<string, string>) => {
      Object.assign(written, map);
    });
    bridge.readSystemPrompt.mockImplementation(async () => ({ ...written }));
    agentBridge.setAgentConfig.mockClear();

    await applyPresetPayload(
      {
        content: { 'AGENTS.md': 'A' },
        character: {
          aiName: '',
          aiAvatar: '',
          userName: '',
          userAvatar: ''
        },
        agent: { tools: ['read_file'], middlewares_disabled: ['TaskIntentMiddleware'] }
      },
      'sess-new'
    );

    expect(agentBridge.setAgentConfig).toHaveBeenCalledWith('sess-new', {
      tools: ['read_file'],
      middlewares_disabled: ['TaskIntentMiddleware']
    });

    // Without a session the call is skipped (no session exists yet).
    agentBridge.setAgentConfig.mockClear();
    await applyPresetPayload({
      content: { 'AGENTS.md': 'A' },
      character: { aiName: '', aiAvatar: '', userName: '', userAvatar: '' }
    });
    expect(agentBridge.setAgentConfig).not.toHaveBeenCalled();
  });

  it('throws when the write cannot be verified (the caller surfaces it)', async () => {
    bridge.writeSystemPrompt.mockResolvedValue(undefined);
    bridge.readSystemPrompt.mockResolvedValue({ 'AGENTS.md': 'something else' });

    await expect(applyPresetPayload(builtinPayload('coding', TEMPLATE, t, NO_AGENT))).rejects.toThrow(
      'applied content verification failed'
    );
    expect(db.cacheCharacter).not.toHaveBeenCalled();
  });
});
