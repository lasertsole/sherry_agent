// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest';
import { createVNode } from 'vue';
import type { ComponentPublicInstance, DirectiveBinding, VNode } from 'vue';

/** Control the lazy KaTeX registration without loading the real plugin. */
const katex = vi.hoisted(() => ({
  ready: false,
  resolve: null as null | (() => void)
}));

vi.mock('../../composables/use-katex', () => ({
  isKatexReady: () => katex.ready,
  registerKatex: () =>
    new Promise<void>(resolve => {
      katex.resolve = () => {
        katex.ready = true;
        resolve();
      };
    })
}));

import { vSafeHtml } from '../safeHtml';

const vnode = createVNode('div') as VNode<any, HTMLElement>;
const binding = (value: string): DirectiveBinding<string | null | undefined> => ({
  instance: {} as ComponentPublicInstance,
  value,
  oldValue: null,
  arg: undefined,
  modifiers: {},
  dir: vSafeHtml
});

describe('vSafeHtml KaTeX readiness', () => {
  it('re-renders a settled element once the lazy plugin becomes ready', async () => {
    const el = document.createElement('div');
    document.body.appendChild(el);
    vSafeHtml.mounted!(el, binding('plain text'), vnode, null);
    const first = el.firstChild;

    await vi.waitFor(() => expect(katex.resolve).not.toBeNull());
    katex.resolve!();

    await vi.waitFor(() => expect(el.firstChild).not.toBe(first));
    expect(el.textContent).toContain('plain text');
    document.body.innerHTML = '';
  });
});
