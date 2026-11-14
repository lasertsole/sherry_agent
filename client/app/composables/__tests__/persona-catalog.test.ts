/**
 * The shared persona preset catalogue — the one source of the preset list, the
 * built-in payloads and the ROLE.md statement, used by the Presets & Role panel, the
 * mandatory new-session dialog and the top-bar preset viewer.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  BUILTIN_PRESETS,
  DEFAULT_PRESET_ID,
  applyPresetPayload,
  builtinAgentConfig,
  builtinPayload,
  catalogEntries,
  composeRoleFile,
  entryName,
  loadPresetPayload,
  userPresetEntryId
} from '@/composables/persona-catalog';
import type { PersonaPreset } from '@/composables/db';

const bridge = vi.hoisted(() => ({
  readSystemPrompt: vi.fn(),
  readSystemPromptTemplate: vi.fn(),
  writeSystemPrompt: vi.fn(),
  writePresetSystemPrompt: vi.fn()
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
    'config.persona.preset.defaultName': '橘雪莉',
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

  it('lists 纯净 / 橘雪莉, with 橘雪莉 the default', () => {
    expect(BUILTIN_PRESETS.map(entry => entry.id)).toEqual(['pure', 'sherry']);
    expect(DEFAULT_PRESET_ID).toBe('sherry');
    const entries = catalogEntries([]);
    expect(entries.map(entry => entry.id)).toEqual(['pure', 'sherry']);
    expect(entries.every(entry => entry.builtin)).toBe(true);
    expect(entries.map(entry => entryName(entry, t))).toEqual(['纯净', '橘雪莉']);
    // Only the default carries the 默认 badge ("Default"); Pure reads 内置 ("Built-in").
    expect(entries.map(entry => t(entry.badgeKey ?? ''))).toEqual(['内置', '默认']);
  });

  it('appends the saved presets after the built-ins', () => {
    const entries = catalogEntries([userPreset(), userPreset({ id: 9, name: 'Second' })]);

    expect(entries.map(entry => entry.id)).toEqual(['pure', 'sherry', userPresetEntryId(7), userPresetEntryId(9)]);
    expect(entryName(entries[2]!, t)).toBe('我的预设');
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
    // Both blank ⇒ no statement at all (the Pure default).
    expect(composeRoleFile(character('', ''), t)).toBe('');
  });

  it('builds the 橘雪莉 payload from the full template and the default roles', () => {
    const payload = builtinPayload('sherry', TEMPLATE, t);

    expect(payload.content['AGENTS.md']).toBe(TEMPLATE['AGENTS.md']);
    expect(payload.content['SOUL.md']).toBe(TEMPLATE['SOUL.md']);
    // The full preset ships no USER profile; the user fills it in later.
    expect(payload.content['USER.md']).toBe('');
    expect(payload.content['ROLE.md']).toBe('# ROLE.md\n\n你将扮演橘雪莉。\n用户将扮演远野汉娜。\n');
    expect(payload.character.aiName).toBe('橘雪莉');
    expect(payload.character.userName).toBe('远野汉娜');
    expect(payload.agent).toEqual({});
  });

  it('builds the 纯净 payload with the full rules but no persona', () => {
    const payload = builtinPayload('pure', TEMPLATE, t);

    // Rules match Full; only SOUL / USER / ROLE and the character stay empty.
    expect(payload.content).toEqual({
      'AGENTS.md': TEMPLATE['AGENTS.md'],
      'SOUL.md': '',
      'USER.md': '',
      'ROLE.md': ''
    });
    expect(payload.character).toEqual({ aiName: '', aiAvatar: '', userName: '', userAvatar: '' });
    expect(payload.agent).toEqual({});
  });

  it('pins no skills for either built-in', () => {
    expect(builtinAgentConfig()).toEqual({});
    for (const id of ['pure', 'sherry'] as const) {
      expect(builtinPayload(id, TEMPLATE, t).agent).toEqual({});
    }
  });

  it('loads a built-in payload from the language template and a saved preset from its row', async () => {
    bridge.readSystemPromptTemplate.mockResolvedValue(TEMPLATE);

    const builtin = await loadPresetPayload(catalogEntries([])[1]!, 'zh', t);
    expect(bridge.readSystemPromptTemplate).toHaveBeenCalledWith('zh');
    expect(builtin.content['AGENTS.md']).toBe(TEMPLATE['AGENTS.md']);

    const user = await loadPresetPayload(catalogEntries([userPreset()])[2]!, 'zh', t);
    expect(user.content).toEqual({
      'AGENTS.md': 'A',
      'SOUL.md': 'S',
      'USER.md': 'U',
      // No character block on the row: the defaults state the roles.
      'ROLE.md': '# ROLE.md\n\n你将扮演橘雪莉。\n用户将扮演远野汉娜。\n'
    });
  });

  it('applies a built-in payload through the preset write route', async () => {
    const written: Record<string, string> = {};
    bridge.writePresetSystemPrompt.mockImplementation(async (map: Record<string, string>) => {
      Object.assign(written, map);
    });
    bridge.readSystemPrompt.mockImplementation(async () => ({ ...written }));
    const payload = builtinPayload('sherry', TEMPLATE, t);

    await applyPresetPayload(payload);

    expect(bridge.writePresetSystemPrompt).toHaveBeenCalledWith(payload.content);
    expect(bridge.writeSystemPrompt).not.toHaveBeenCalled();
    expect(db.cacheCharacter).toHaveBeenCalledWith({
      session_id: '__global__',
      aiName: '橘雪莉',
      aiAvatar: '/avatar/assistant.jpg',
      userName: '远野汉娜',
      userAvatar: '/avatar/user.jpg'
    });
  });

  it('applies an ordinary persona payload through the community write route', async () => {
    const payload = {
      content: { 'SOUL.md': 'S', 'USER.md': 'U', 'ROLE.md': 'R' },
      character: { aiName: '', aiAvatar: '', userName: '', userAvatar: '' }
    };
    bridge.writeSystemPrompt.mockResolvedValue(undefined);
    bridge.readSystemPrompt.mockResolvedValue(payload.content);

    await applyPresetPayload(payload);

    expect(bridge.writeSystemPrompt).toHaveBeenCalledWith(payload.content);
    expect(bridge.writePresetSystemPrompt).not.toHaveBeenCalled();
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
        content: { 'SOUL.md': 'S' },
        character: {
          aiName: '',
          aiAvatar: '',
          userName: '',
          userAvatar: ''
        },
        agent: { skills: ['clawhub'] }
      },
      'sess-new'
    );

    expect(agentBridge.setAgentConfig).toHaveBeenCalledWith('sess-new', { skills: ['clawhub'] });

    // Without a session the call is skipped (no session exists yet).
    agentBridge.setAgentConfig.mockClear();
    await applyPresetPayload({
      content: { 'SOUL.md': 'S' },
      character: { aiName: '', aiAvatar: '', userName: '', userAvatar: '' }
    });
    expect(agentBridge.setAgentConfig).not.toHaveBeenCalled();
  });

  it('throws when the write cannot be verified (the caller surfaces it)', async () => {
    bridge.writePresetSystemPrompt.mockResolvedValue(undefined);
    bridge.readSystemPrompt.mockResolvedValue({ 'AGENTS.md': 'something else' });

    await expect(applyPresetPayload(builtinPayload('sherry', TEMPLATE, t))).rejects.toThrow(
      'applied content verification failed'
    );
    expect(db.cacheCharacter).not.toHaveBeenCalled();
  });
});
