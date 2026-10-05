/**
 * The shared persona preset catalogue — the one source of the preset list, the
 * built-in payloads and the ROLE.md statement, used by the 预设 panel, the
 * mandatory new-session dialog and the top-bar preset viewer.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  BUILTIN_PRESETS,
  DEFAULT_PRESET_ID,
  applyPresetPayload,
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
  writeSystemPrompt: vi.fn()
}));
vi.mock('~/composables/bridge', () => bridge);

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
    'config.persona.preset.builtinCodingName': '编程助手',
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

  it('leads with 编程助手 as the default, 橘雪莉 behind it as the built-in', () => {
    expect(BUILTIN_PRESETS.map(entry => entry.id)).toEqual(['coding', 'sherry']);
    expect(DEFAULT_PRESET_ID).toBe('coding');
    const entries = catalogEntries([]);
    expect(entries.map(entry => entry.id)).toEqual(['coding', 'sherry']);
    expect(entries.every(entry => entry.builtin)).toBe(true);
    expect(entryName(entries[0]!, t)).toBe('编程助手');
    expect(entryName(entries[1]!, t)).toBe('橘雪莉');
  });

  it('appends the saved presets after the built-ins', () => {
    const entries = catalogEntries([userPreset(), userPreset({ id: 9, name: 'Second' })]);

    expect(entries.map(entry => entry.id)).toEqual(['coding', 'sherry', userPresetEntryId(7), userPresetEntryId(9)]);
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
    // Both blank ⇒ no statement at all (the 编程助手 default).
    expect(composeRoleFile(character('', ''), t)).toBe('');
  });

  it('builds the 编程助手 payload with no roles and no soul / user profile', () => {
    const payload = builtinPayload('coding', TEMPLATE, t);

    expect(payload.content).toEqual({
      'AGENTS.md': TEMPLATE['AGENTS.md'],
      'SOUL.md': '',
      'USER.md': '',
      'ROLE.md': ''
    });
    expect(payload.character).toEqual({
      aiName: '',
      aiAvatar: '/avatar/assistant.jpg',
      userName: '',
      userAvatar: '/avatar/user.jpg'
    });
  });

  it('builds the 橘雪莉 payload from the full template and the default roles', () => {
    const payload = builtinPayload('sherry', TEMPLATE, t);

    expect(payload.content['AGENTS.md']).toBe(TEMPLATE['AGENTS.md']);
    expect(payload.content['SOUL.md']).toBe(TEMPLATE['SOUL.md']);
    expect(payload.content['USER.md']).toBe(TEMPLATE['USER.md']);
    expect(payload.content['ROLE.md']).toBe('# ROLE.md\n\n你将扮演橘雪莉。\n用户将扮演远野汉娜。\n');
    expect(payload.character.aiName).toBe('橘雪莉');
    expect(payload.character.userName).toBe('远野汉娜');
  });

  it('loads a built-in payload from the language template and a saved preset from its row', async () => {
    bridge.readSystemPromptTemplate.mockResolvedValue(TEMPLATE);

    const builtin = await loadPresetPayload(catalogEntries([])[0]!, 'zh', t);
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

  it('applies a payload through the API (verified) and the global character profile', async () => {
    const written: Record<string, string> = {};
    bridge.writeSystemPrompt.mockImplementation(async (map: Record<string, string>) => {
      Object.assign(written, map);
    });
    bridge.readSystemPrompt.mockImplementation(async () => ({ ...written }));
    const payload = builtinPayload('coding', TEMPLATE, t);

    await applyPresetPayload(payload);

    expect(bridge.writeSystemPrompt).toHaveBeenCalledWith(payload.content);
    expect(db.cacheCharacter).toHaveBeenCalledWith({
      session_id: '__global__',
      aiName: '',
      aiAvatar: '/avatar/assistant.jpg',
      userName: '',
      userAvatar: '/avatar/user.jpg'
    });
  });

  it('throws when the write cannot be verified (the caller surfaces it)', async () => {
    bridge.writeSystemPrompt.mockResolvedValue(undefined);
    bridge.readSystemPrompt.mockResolvedValue({ 'AGENTS.md': 'something else' });

    await expect(applyPresetPayload(builtinPayload('coding', TEMPLATE, t))).rejects.toThrow(
      'applied content verification failed'
    );
    expect(db.cacheCharacter).not.toHaveBeenCalled();
  });
});
