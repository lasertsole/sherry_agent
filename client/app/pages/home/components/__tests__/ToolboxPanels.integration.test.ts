/**
 * The toolbox's two working surfaces: the browser (an address bar over an
 * embedded page) and the user's terminal (commands typed by the operator).
 *
 * The browser's state lives in the toolbox store because the sidebar unmounts an
 * inactive panel — the suite therefore drives navigation through the store and
 * asserts the panel renders what it holds. The terminal half pins the round trip:
 * Enter sends the line to `POST /terminal/run` with the session id, the result
 * lands in the scrollback (with its exit code) and survives a remount.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const bridge = vi.hoisted(() => ({
  fetchTerminalInfo: vi.fn(),
  runTerminalCommand: vi.fn(),
  // The panel asks this on mount; a default install answers {"enabled": false}
  // and the panel keeps its iframe mode (the CDP suite flips it per test).
  fetchBrowserStatus: vi.fn(async () => ({ enabled: false, running: false, pages: 0 }))
}));
vi.mock('~/composables/bridge/toolbox', () => bridge);

/**
 * The CDP channel is replaced by a recorder: the panel's contract is "connect
 * once, forward frames/shape commands", and the socket itself has its own suite.
 */
const channels = vi.hoisted(() => {
  interface Sent {
    [key: string]: unknown;
  }

  class FakeChannel {
    static instances: FakeChannel[] = [];

    handlers: Record<string, (...args: never[]) => void>;

    sent: Sent[] = [];

    disposed = false;

    connected = false;

    constructor(_sessionId: string, handlers: Record<string, (...args: never[]) => void>) {
      this.handlers = handlers;
      FakeChannel.instances.push(this);
    }

    connect() {
      this.connected = true;
    }

    dispose() {
      this.disposed = true;
      this.connected = false;
    }

    send(payload: Sent) {
      this.sent.push(payload);
      return true;
    }
  }
  return { FakeChannel };
});
vi.mock('~/composables/browser-channel', () => ({ BrowserChannel: channels.FakeChannel }));
vi.mock('vue-router', () => ({ useRoute: () => ({ params: { sid: 'sid-1' } }) }));

import BrowserPanel from '@/pages/home/components/BrowserPanel.vue';
import TerminalPanel from '@/pages/home/components/TerminalPanel.vue';
import { useToolboxStore } from '@/stores/toolbox';

const stubs = {
  Button: {
    name: 'Button',
    props: ['icon', 'title', 'disabled', 'loading'],
    emits: ['click'],
    // A DECLARED prop does not fall through to the DOM, so the title is bound
    // explicitly (the devtools assertion reads it).
    template: '<button class="btn" :title="title" :disabled="disabled" @click="$emit(\'click\')" />'
  },
  InputText: {
    name: 'InputText',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="inp" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="spin" />' }
};

describe('BrowserPanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('navigates from the address bar and renders the page in a frame', async () => {
    const wrapper = mount(BrowserPanel, { global: { stubs } });
    expect(wrapper.get('[data-test="browser-empty"]').exists()).toBe(true);

    await wrapper.get('[data-test="browser-address"]').setValue('example.com');
    await wrapper.get('[data-test="browser-go"]').trigger('click');

    expect(wrapper.get('[data-test="browser-frame"]').attributes('src')).toBe('https://example.com');
  });

  it('walks the history with the back and forward buttons', async () => {
    const store = useToolboxStore();
    store.navigate('sid-1::browser', 'a.com');
    store.navigate('sid-1::browser', 'b.com');
    const wrapper = mount(BrowserPanel, { global: { stubs } });
    await flushPromises();

    // Forward is dead at the newest page, back is live.
    expect(wrapper.get('[data-test="browser-forward"]').attributes('disabled')).toBeDefined();
    await wrapper.get('[data-test="browser-back"]').trigger('click');
    expect(wrapper.get('[data-test="browser-frame"]').attributes('src')).toBe('https://a.com');
    expect(wrapper.get('[data-test="browser-back"]').attributes('disabled')).toBeDefined();

    await wrapper.get('[data-test="browser-forward"]').trigger('click');
    expect(wrapper.get('[data-test="browser-frame"]').attributes('src')).toBe('https://b.com');
  });

  it('switches to a fixed device frame and resizes it from the handles', async () => {
    const store = useToolboxStore();
    store.navigate('sid-1::browser', 'example.com');
    const wrapper = mount(BrowserPanel, { global: { stubs } });

    // Free size is off until the toggle is pressed: the page fills the panel.
    expect(wrapper.find('[data-test="browser-free-frame"]').exists()).toBe(false);

    await wrapper.get('[data-test="browser-free-size"]').trigger('click');

    // The frame renders at ZCode's device default (393×852) with eight handles.
    const frame = wrapper.get('[data-test="browser-free-frame"]');
    expect(frame.attributes('style')).toContain('width: 393px');
    expect(frame.attributes('style')).toContain('height: 852px');
    expect(wrapper.findAll('[data-test^="browser-resize-"]')).toHaveLength(8);
    // The frame scales down to fit the panel (no layout in happy-dom → scale 1).
    expect(wrapper.get('[data-responsive-width]').attributes('data-responsive-width')).toBe('393');

    // The right-hand edge handle grows the frame by the pointer delta.
    await wrapper.get('[data-test="browser-resize-right"]').trigger('pointerdown', {
      clientX: 100,
      clientY: 100,
      pointerId: 1,
      pointerType: 'mouse',
      button: 0,
      buttons: 1
    });
    await wrapper.get('[data-test="browser-resize-right"]').trigger('pointermove', {
      clientX: 160,
      clientY: 100,
      pointerId: 1,
      pointerType: 'mouse',
      buttons: 1
    });
    await wrapper.get('[data-test="browser-resize-right"]').trigger('pointerup', { pointerId: 1 });

    expect(store.browserFor('sid-1::browser').viewport).toEqual({ width: 453, height: 852 });

    // A drag past the floor clamps at ZCode's minimum (320×320).
    await wrapper.get('[data-test="browser-resize-right"]').trigger('pointerdown', {
      clientX: 0,
      clientY: 0,
      pointerId: 2,
      pointerType: 'mouse',
      button: 0,
      buttons: 1
    });
    await wrapper.get('[data-test="browser-resize-right"]').trigger('pointermove', {
      clientX: -5000,
      clientY: 0,
      pointerId: 2,
      pointerType: 'mouse',
      buttons: 1
    });
    await wrapper.get('[data-test="browser-resize-right"]').trigger('pointerup', { pointerId: 2 });
    expect(store.browserFor('sid-1::browser').viewport.width).toBe(320);

    // Keyboard resize: Shift+ArrowRight moves 10px (the handles are focusable).
    const handle = wrapper.get('[data-test="browser-resize-right"]');
    await handle.trigger('keydown', { key: 'ArrowRight', shiftKey: true });
    expect(store.browserFor('sid-1::browser').viewport.width).toBe(330);

    // Leaving free size restores the full-panel page.
    await wrapper.get('[data-test="browser-free-size"]').trigger('click');
    expect(wrapper.find('[data-test="browser-free-frame"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="browser-frame"]').exists()).toBe(true);
  });

  it('sizes the frame from the width / height inputs and the zoom picker', async () => {
    const store = useToolboxStore();
    store.navigate('sid-1::browser', 'example.com');
    const wrapper = mount(BrowserPanel, { global: { stubs } });

    // The row shows only in free-size mode (it is where the device is set up).
    expect(wrapper.find('[data-test="browser-size-row"]').exists()).toBe(false);
    await wrapper.get('[data-test="browser-free-size"]').trigger('click');

    const row = wrapper.get('[data-test="browser-size-row"]');
    expect(row.exists()).toBe(true);
    const widthInput = wrapper.get('[data-test="browser-width-input"]');
    const heightInput = wrapper.get('[data-test="browser-height-input"]');
    // The inputs mirror the current frame.
    expect((widthInput.element as HTMLInputElement).value).toBe('393');
    expect((heightInput.element as HTMLInputElement).value).toBe('852');

    // Typing is a DRAFT: the `input` events of a half-typed number must not resize
    // the frame — only the committed `change` does (VTU's setValue would fire both).
    (widthInput.element as HTMLInputElement).value = '414';
    await widthInput.trigger('input');
    expect(store.browserFor('sid-1::browser').viewport.width).toBe(393);
    await widthInput.trigger('change');
    expect(store.browserFor('sid-1::browser').viewport).toEqual({ width: 414, height: 852 });
    expect(wrapper.get('[data-test="browser-free-frame"]').attributes('style')).toContain('width: 414px');

    // Out-of-band values are clamped and echoed back into the input.
    // An out-of-band value is clamped and echoed back into the box.
    (widthInput.element as HTMLInputElement).value = '99';
    await widthInput.trigger('input');
    await widthInput.trigger('change');
    expect(store.browserFor('sid-1::browser').viewport.width).toBe(320);
    expect((widthInput.element as HTMLInputElement).value).toBe('320');

    // The zoom picker sets an explicit scale (and `fit` hands it back to the panel).
    const zoomInput = wrapper.get('[data-test="browser-zoom-input"]');
    expect((zoomInput.element as HTMLSelectElement).value).toBe('fit');
    await zoomInput.setValue('50');
    await zoomInput.trigger('change');
    expect(store.browserFor('sid-1::browser').zoom).toBe('50');
    expect(wrapper.get('[data-test="browser-free-frame"]').attributes('style')).toContain('width: 160px');
    // The zoom rides with the instance: a second browser stays at `fit`.
    expect(store.browserFor('sid-1::browser#2').zoom).toBe('fit');
  });

  it('opens the page in a real window for devtools (an iframe cannot host them)', async () => {
    const store = useToolboxStore();
    store.navigate('sid-1::browser', 'example.com');
    const opened: string[] = [];
    vi.stubGlobal('open', (url: string) => {
      opened.push(url);
      return null;
    });
    const wrapper = mount(BrowserPanel, { global: { stubs } });

    const button = wrapper.get('[data-test="browser-devtools"]');
    expect(button.attributes('title')).toContain('跨域 iframe');
    await button.trigger('click');

    expect(opened).toEqual(['https://example.com']);
    vi.unstubAllGlobals();
    vi.stubGlobal('useRightSidebarStore', () => ({}));
  });

  it('re-mounts the frame on reload so the same URL is fetched again', async () => {
    const store = useToolboxStore();
    store.navigate('sid-1::browser', 'a.com');
    const wrapper = mount(BrowserPanel, { global: { stubs } });

    const before = wrapper.get('[data-test="browser-frame"]').element as HTMLIFrameElement;
    await wrapper.get('[data-test="browser-reload"]').trigger('click');
    const after = wrapper.get('[data-test="browser-frame"]').element as HTMLIFrameElement;

    // A fresh node: setting `src` to the same value is a no-op for the browser.
    expect(after).not.toBe(before);
    expect(after.src).toContain('a.com');
  });
});

describe('TerminalPanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    bridge.fetchTerminalInfo.mockReset();
    bridge.runTerminalCommand.mockReset();
    bridge.fetchTerminalInfo.mockResolvedValue({ cwd: '/tmp/project', shell: '/bin/sh' });
  });

  it('shows the working directory the session resolved to', async () => {
    const wrapper = mount(TerminalPanel, { global: { stubs } });
    await flushPromises();

    expect(bridge.fetchTerminalInfo).toHaveBeenCalledWith('sid-1');
    expect(wrapper.get('[data-test="terminal-cwd"]').text()).toContain('/tmp/project');
    expect(wrapper.get('[data-test="terminal-empty"]').exists()).toBe(true);
  });

  it('runs a typed command and appends its output and exit code', async () => {
    bridge.runTerminalCommand.mockResolvedValueOnce({
      cwd: '/tmp/project',
      command: 'ls',
      exit_code: 0,
      output: 'a.txt\nb.txt',
      truncated: false,
      duration_ms: 12
    });
    const wrapper = mount(TerminalPanel, { global: { stubs } });
    await flushPromises();

    await wrapper.get('[data-test="terminal-input"]').setValue('ls');
    await wrapper.get('[data-test="terminal-input"]').trigger('keyup.enter');
    await flushPromises();

    expect(bridge.runTerminalCommand).toHaveBeenCalledWith('sid-1', 'ls');
    const entry = wrapper.get('[data-test="terminal-entry-0"]');
    expect(entry.text()).toContain('ls');
    expect(entry.text()).toContain('a.txt');
    expect(entry.text()).toContain('退出码 0');
    // The line is cleared for the next command.
    expect((wrapper.get('[data-test="terminal-input"]').element as HTMLInputElement).value).toBe('');
  });

  it('reports a non-zero exit code and keeps the scrollback across remounts', async () => {
    bridge.runTerminalCommand.mockResolvedValueOnce({
      cwd: '/tmp/project',
      command: 'exit 3',
      exit_code: 3,
      output: 'boom',
      truncated: true,
      duration_ms: 4
    });
    const first = mount(TerminalPanel, { global: { stubs } });
    await flushPromises();
    await first.get('[data-test="terminal-input"]').setValue('exit 3');
    await first.get('[data-test="terminal-input"]').trigger('keyup.enter');
    await flushPromises();
    expect(first.get('[data-test="terminal-entry-0"]').text()).toContain('退出码 3');
    expect(first.text()).toContain('输出已截断');

    // Remounting (a tab switch) keeps the log — it lives in the store.
    const second = mount(TerminalPanel, { global: { stubs } });
    await flushPromises();
    expect(second.get('[data-test="terminal-entry-0"]').text()).toContain('exit 3');
  });

  it('clears the scrollback on request', async () => {
    bridge.runTerminalCommand.mockResolvedValueOnce({
      cwd: '/tmp/project',
      command: 'ls',
      exit_code: 0,
      output: 'x',
      truncated: false,
      duration_ms: 1
    });
    const wrapper = mount(TerminalPanel, { global: { stubs } });
    await flushPromises();
    await wrapper.get('[data-test="terminal-input"]').setValue('ls');
    await wrapper.get('[data-test="terminal-input"]').trigger('keyup.enter');
    await flushPromises();

    await wrapper.get('[data-test="terminal-clear"]').trigger('click');

    expect(wrapper.find('[data-test="terminal-entry-0"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="terminal-empty"]').exists()).toBe(true);
  });

  it('keeps two terminal instances apart (the toolbox opens one per click)', async () => {
    bridge.runTerminalCommand.mockResolvedValue({
      cwd: '/tmp/project',
      command: 'ls',
      exit_code: 0,
      output: 'x',
      truncated: false,
      duration_ms: 1
    });
    const first = mount(TerminalPanel, {
      props: { payload: { instance: 'terminal#1' } },
      global: { stubs }
    });
    const second = mount(TerminalPanel, {
      props: { payload: { instance: 'terminal#2' } },
      global: { stubs }
    });
    await flushPromises();

    await first.get('[data-test="terminal-input"]').setValue('ls');
    await first.get('[data-test="terminal-input"]').trigger('keyup.enter');
    await flushPromises();

    expect(first.get('[data-test="terminal-entry-0"]').exists()).toBe(true);
    // The other instance kept its own (empty) scrollback.
    expect(second.find('[data-test="terminal-entry-0"]').exists()).toBe(false);
    expect(second.get('[data-test="terminal-empty"]').exists()).toBe(true);
  });

  it('does not run an empty line', async () => {
    const wrapper = mount(TerminalPanel, { global: { stubs } });
    await flushPromises();

    await wrapper.get('[data-test="terminal-input"]').setValue('   ');
    await wrapper.get('[data-test="terminal-input"]').trigger('keyup.enter');
    await flushPromises();

    expect(bridge.runTerminalCommand).not.toHaveBeenCalled();
  });
});

describe('BrowserPanel — CDP mode', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    channels.FakeChannel.instances = [];
    bridge.fetchBrowserStatus.mockResolvedValue({ enabled: true, running: true, pages: 1 });
  });

  /** Mount the panel in CDP mode with its channel ready. */
  async function mountCdp() {
    const wrapper = mount(BrowserPanel, { global: { stubs } });
    await flushPromises();
    const channel = channels.FakeChannel.instances[0]!;
    return { wrapper, channel };
  }

  it('switches to the frame stream and renders the picture', async () => {
    const { wrapper, channel } = await mountCdp();

    expect(channel.connected).toBe(true);
    channel.handlers.onReady!({
      enabled: true,
      running: true,
      page: 'p1',
      url: 'https://x.test',
      title: 'X',
      can_back: true,
      can_forward: false,
      kind: 'page'
    } as never);
    channel.handlers.onFrame!({ data: 'AAA', width: 393, height: 852 } as never);
    await flushPromises();

    const frame = wrapper.get('[data-test="browser-frame"]');
    expect(frame.element.tagName).toBe('IMG');
    expect(frame.attributes('src')).toBe('data:image/jpeg;base64,AAA');
    // The address bar mirrors the server's page, and back follows its history.
    expect(wrapper.get('[data-test="browser-address"]').element.value).toBe('https://x.test');
    expect(wrapper.get('[data-test="browser-back"]').attributes('disabled')).toBeUndefined();
    expect(wrapper.get('[data-test="browser-forward"]').attributes('disabled')).toBeDefined();
    wrapper.unmount();
    expect(channel.disposed).toBe(true);
  });

  it('sends navigation and pointer events over the channel', async () => {
    const { wrapper, channel } = await mountCdp();

    await wrapper.get('[data-test="browser-address"]').setValue('example.com');
    await wrapper.get('[data-test="browser-go"]').trigger('click');
    expect(channel.sent).toContainEqual({ event: 'nav', url: 'https://example.com' });

    channel.handlers.onFrame!({ data: 'AAA', width: 393, height: 852 } as never);
    await flushPromises();
    await wrapper.get('[data-test="browser-frame"]').trigger('pointerdown', {
      clientX: 10,
      clientY: 20,
      button: 0,
      buttons: 1,
      pointerType: 'mouse'
    });
    const click = channel.sent.find(item => item.event === 'input');
    expect(click).toMatchObject({ kind: 'mouse' });
    expect((click as { payload: Record<string, unknown> }).payload).toMatchObject({
      type: 'mousePressed',
      x: 10,
      y: 20,
      clickCount: 1
    });
    wrapper.unmount();
  });

  it('opens the inspector target and toggles back', async () => {
    const { wrapper, channel } = await mountCdp();
    channel.handlers.onPage!({
      page: 'p1',
      url: 'https://x.test',
      title: 'X',
      can_back: false,
      can_forward: false,
      kind: 'page'
    } as never);
    channel.handlers.onFrame!({ data: 'AAA', width: 393, height: 852 } as never);
    await flushPromises();

    await wrapper.get('[data-test="browser-devtools"]').trigger('click');
    expect(channel.sent).toContainEqual({ event: 'devtools', on: true });

    // The server confirms with a devtools page frame; the button flips back.
    channel.handlers.onPage!({
      page: 'p9',
      url: 'devtools://devtools/bundled/inspector.html',
      title: 'DevTools',
      can_back: false,
      can_forward: false,
      kind: 'devtools'
    } as never);
    await flushPromises();
    // The suite runs under the zh locale (the inline i18n block's first entry).
    expect(wrapper.get('[data-test="browser-devtools"]').attributes('title')).toBe('返回页面');

    await wrapper.get('[data-test="browser-devtools"]').trigger('click');
    expect(channel.sent).toContainEqual({ event: 'devtools', on: false });
    wrapper.unmount();
  });

  it('shows the reconnecting hint and refuses commands while offline', async () => {
    const { wrapper, channel } = await mountCdp();
    channel.handlers.onFrame!({ data: 'AAA', width: 393, height: 852 } as never);
    await flushPromises();
    expect(wrapper.find('[data-test="browser-status"]').exists()).toBe(false);

    channel.handlers.onOpen!();
    await flushPromises();
    expect(wrapper.find('[data-test="browser-status"]').exists()).toBe(false);

    channel.handlers.onClose!();
    await flushPromises();
    // zh locale: the hint is the inline block's reconnecting string.
    expect(wrapper.get('[data-test="browser-status"]').text()).toContain('正在重连');

    channel.handlers.onError!('unknown ref' as never);
    await flushPromises();
    expect(wrapper.get('[data-test="browser-status"]').text()).toContain('unknown ref');
    wrapper.unmount();
  });

  it('keeps the iframe panel when the feature is off', async () => {
    bridge.fetchBrowserStatus.mockResolvedValue({ enabled: false, running: false, pages: 0 });
    const wrapper = mount(BrowserPanel, { global: { stubs } });
    await flushPromises();

    expect(channels.FakeChannel.instances).toHaveLength(0);
    await wrapper.get('[data-test="browser-address"]').setValue('example.com');
    await wrapper.get('[data-test="browser-go"]').trigger('click');
    expect(wrapper.get('[data-test="browser-frame"]').element.tagName).toBe('IFRAME');
    wrapper.unmount();
  });

  it('maps pointer coordinates through the emulated viewport, not the frame pixels', async () => {
    const { wrapper, channel } = await mountCdp();
    // Free size emulates a device server-side; Chrome then reports its own frame
    // pixel size (measured 393x852 -> 554x1200). Coordinates must stay in the
    // emulated CSS space or every click lands off-target.
    const store = useToolboxStore();
    // The free-size branch renders once the page has a URL (the server's page frame).
    channel.handlers.onPage!({
      page: 'p1',
      url: 'https://example.com/',
      title: 'Example',
      can_back: false,
      can_forward: false,
      kind: 'page'
    } as never);
    store.setBrowserResponsive('sid-1::browser', true);
    store.setBrowserViewport('sid-1::browser', { width: 393, height: 852 });
    channel.handlers.onFrame!({ data: 'AAA', width: 554, height: 1200 } as never);
    await flushPromises();

    const rendered = wrapper.get('[data-test="browser-frame"]');
    // The free-size picture must be focusable: without a tabindex the click
    // cannot move focus onto it and every forwarded key goes astray.
    expect(rendered.attributes('tabindex')).toBe('0');
    // happy-dom reports a zero-size rect, so the render scale is 1: screen x
    // becomes the page's CSS x directly.
    await rendered.trigger('pointerdown', { clientX: 120, clientY: 240, button: 0, buttons: 1, pointerType: 'mouse' });
    const click = channel.sent.filter(item => item.event === 'input').pop() as { payload: Record<string, unknown> };
    expect(click.payload).toMatchObject({ type: 'mousePressed', x: 120, y: 240 });
    wrapper.unmount();
  });
});
