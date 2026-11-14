/**
 * 系统配置 → LSP / 代码智能: the merged read, the project-layer editor,
 * save/delete/test calls and the upload input's presence.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

import LspConfigSection from '../LspConfigSection.vue';

const api = vi.hoisted(() => ({
  fetchApi: vi.fn(),
  toastSuccess: vi.fn(),
  toastError: vi.fn()
}));

vi.mock('@/composables/requestApi', () => ({ fetchApi: api.fetchApi }));
vi.mock('@/composables/toast', () => ({
  toastSuccess: api.toastSuccess,
  toastError: api.toastError,
  toastWarn: vi.fn()
}));

const CONFIG = {
  success: true,
  session_id: 's1',
  root: '/proj',
  project_path: '/proj/.sherry/lsp.json',
  servers: [
    {
      language: 'python',
      command: ['basedpyright-langserver', '--stdio'],
      extensions: ['.py'],
      language_id: 'python',
      install_hint: '',
      local_install: null,
      source: 'builtin',
      enabled: true,
      installed: true,
      binary_path: '/bin/basedpyright-langserver'
    },
    {
      language: 'elixir',
      command: ['elixir-ls'],
      extensions: ['.ex'],
      language_id: 'elixir',
      install_hint: 'install elixir-ls',
      local_install: null,
      source: 'project',
      enabled: true,
      installed: false,
      binary_path: ''
    }
  ],
  enabled: ['python', 'elixir'],
  project: {
    servers: { elixir: { command: ['elixir-ls'], extensions: ['.ex'], language_id: 'elixir' } },
    enable: ['elixir']
  },
  errors: []
};

const calls: Array<{ url: string; method?: string; opts?: Record<string, unknown> }> = [];

beforeEach(() => {
  calls.length = 0;
  api.toastSuccess.mockReset();
  api.toastError.mockReset();
  (globalThis as any).useRoute.mockReturnValue({ params: { sid: 's1' } });
  api.fetchApi.mockReset().mockImplementation(async (params: any) => {
    const url = String(params.url);
    calls.push({ url, method: params.method, opts: params.opts });
    if (url.startsWith('/lsp/config/project') && params.method === 'put') return { success: true };
    if (url.startsWith('/lsp/config/project') && params.method === 'delete') return { success: true };
    if (url.startsWith('/lsp/config/project') && params.method === 'post') return { success: true };
    if (url.startsWith('/lsp/config/project') && params.method === 'get') {
      return { success: true, root: '/proj', path: '/proj/.sherry/lsp.json', exists: true, ...CONFIG.project, errors: [] };
    }
    if (url.startsWith('/lsp/config/test')) {
      return { success: true, ok: true, language: 'elixir', message: 'initialized' };
    }
    if (url.startsWith('/lsp/config')) return CONFIG;
    return null;
  });
});

const stubs = {
  Button: {
    name: 'Button',
    props: ['label', 'loading'],
    emits: ['click'],
    template: '<button class="btn" @click="$emit(\'click\')">{{ label }}</button>'
  },
  InputText: {
    name: 'InputText',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="it" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  }
};

async function mountSection() {
  const wrapper = mount(LspConfigSection, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

describe('LspConfigSection', () => {
  it('renders the merged global table and the project rows', async () => {
    const wrapper = await mountSection();

    expect(wrapper.find('[data-test="lsp-global-python"]').exists()).toBe(true);
    // A project entry is shown in the editable table, not the read-only one.
    expect(wrapper.find('[data-test="lsp-global-elixir"]').exists()).toBe(false);
    const rows = wrapper.findAll('[data-test="lsp-project-row"]');
    expect(rows).toHaveLength(1);
    expect((wrapper.find('[data-test="lsp-row-command"]').element as HTMLInputElement).value).toBe(
      'elixir-ls'
    );
  });

  it('saves the edited project layer', async () => {
    const wrapper = await mountSection();
    await wrapper.find('[data-test="lsp-row-command"]').setValue('elixir-ls --stdio');
    await wrapper.find('[data-test="lsp-save"]').trigger('click');
    await flushPromises();

    const put = calls.find(call => call.method === 'put');
    expect(put).toBeTruthy();
    const servers = (put?.opts as any)?.servers;
    expect(servers.elixir.command).toEqual(['elixir-ls', '--stdio']);
    expect(api.toastSuccess).toHaveBeenCalled();
  });

  it('adds an empty row and includes it on save', async () => {
    const wrapper = await mountSection();
    await wrapper.find('[data-test="lsp-add"]').trigger('click');
    const rows = wrapper.findAll('[data-test="lsp-project-row"]');
    expect(rows).toHaveLength(2);

    const inputs = wrapper.findAll('[data-test="lsp-row-language"]');
    await inputs[1]!.setValue('haskell');
    const commands = wrapper.findAll('[data-test="lsp-row-command"]');
    await commands[1]!.setValue('haskell-language-server --lsp');
    const extensions = wrapper.findAll('[data-test="lsp-row-extensions"]');
    await extensions[1]!.setValue('.hs');
    await wrapper.find('[data-test="lsp-save"]').trigger('click');
    await flushPromises();

    const put = calls.find(call => call.method === 'put');
    expect((put?.opts as any)?.servers.haskell.command).toEqual([
      'haskell-language-server',
      '--lsp'
    ]);
  });

  it('tests one server through the backend probe', async () => {
    const wrapper = await mountSection();
    await wrapper.find('[data-test="lsp-row-test"]').trigger('click');
    await flushPromises();

    const post = calls.find(call => call.url.startsWith('/lsp/config/test'));
    expect((post?.opts as any)?.language).toBe('elixir');
    expect(api.toastSuccess).toHaveBeenCalledWith(expect.anything(), 'initialized');
  });

  it('deletes a project entry', async () => {
    const wrapper = await mountSection();
    await wrapper.find('[data-test="lsp-row-delete"]').trigger('click');
    await flushPromises();

    const remove = calls.find(call => call.method === 'delete');
    expect((remove?.opts as any)?.language).toBe('elixir');
  });

  it('offers a file input for lsp.json uploads', async () => {
    const wrapper = await mountSection();
    expect(wrapper.find('[data-test="lsp-upload-input"]').exists()).toBe(true);
  });
});
