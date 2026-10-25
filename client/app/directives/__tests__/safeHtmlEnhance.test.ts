// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createVNode } from 'vue';
import type { ComponentPublicInstance, DirectiveBinding, VNode } from 'vue';
import { vSafeHtml, safeMarkdownHtml } from '../safeHtml';
import { _resetShikiForTest, resolveShikiLang } from '../../composables/use-shiki';

/**
 * The enhancement pass: a settled element's ```fences are upgraded to Shiki
 * blocks (with the toolbar), mermaid fences to figures, and a STREAMING
 * element (data-streaming="true") pays nothing. Shiki/KaTeX run for real
 * (pure-JS engines); mermaid is mocked at its module seam.
 */
const mermaid = vi.hoisted(() => ({ render: vi.fn(), initialize: vi.fn() }));
vi.mock('mermaid', () => ({
  default: {
    initialize: mermaid.initialize,
    render: mermaid.render
  }
}));

const formulaPreview = vi.hoisted(() => ({ open: vi.fn() }));
vi.mock('~/composables/use-formula-preview', () => ({
  useFormulaPreview: () => ({ openFormulaPreview: formulaPreview.open })
}));

const vnode = createVNode('div') as VNode<any, HTMLElement>;
const binding = (
  value: string,
  modifiers: Record<string, boolean> = {}
): DirectiveBinding<string | null | undefined> => ({
  instance: {} as ComponentPublicInstance,
  value,
  oldValue: null,
  arg: undefined,
  modifiers,
  dir: vSafeHtml
});

async function waitEnhanced() {
  // The pass is fire-and-forget async (dynamic imports + shiki) — poll the DOM.
  await vi.waitFor(
    () => {
      if (!document.querySelector('pre.shiki') && !document.querySelector('.mermaid-figure')) {
        throw new Error('not enhanced yet');
      }
    },
    { timeout: 8000, interval: 100 }
  );
}

describe('safeHtml enhancement pass（代码块/mermaid/流式门）', () => {
  beforeEach(() => {
    _resetShikiForTest();
    document.body.innerHTML = '';
    mermaid.render.mockReset();
    mermaid.initialize.mockReset();
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn(async () => undefined) } });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    document.body.innerHTML = '';
  });

  it('settle 后把 fence 升级为带工具栏的 shiki 块（真渲染）', async () => {
    const el = document.createElement('div');
    document.body.appendChild(el);
    vSafeHtml.mounted!(el, binding('```ts\nconst x: number = 1;\n```'), vnode, null as never);
    await waitEnhanced();

    const shiki = el.querySelector('pre.shiki');
    expect(shiki).not.toBeNull();
    expect(shiki!.querySelector('span[style*="--shiki-light"]')).not.toBeNull();
    expect(el.querySelector('.code-toolbar .code-lang')?.textContent).toBe('ts');
    expect(el.querySelector('[data-act="copy"]')).not.toBeNull();
    expect(el.querySelector('[data-act="wrap"]')).not.toBeNull();
  });

  it('流式期间（data-streaming）不发起增强', async () => {
    const el = document.createElement('div');
    el.dataset.streaming = 'true';
    document.body.appendChild(el);
    vSafeHtml.mounted!(el, binding('```ts\nconst x = 1;\n```'), vnode, null as never);
    await new Promise(resolve => setTimeout(resolve, 800));

    expect(el.querySelector('pre.shiki')).toBeNull();
    expect(el.querySelector('pre code')?.textContent).toContain('const x = 1;');
  });

  it('停流后 updated 渲染触发增强', async () => {
    const el = document.createElement('div');
    el.dataset.streaming = 'true';
    document.body.appendChild(el);
    vSafeHtml.mounted!(el, binding('```ts\nconst x = 1;\n```'), vnode, null as never);
    await new Promise(resolve => setTimeout(resolve, 400));

    delete el.dataset.streaming;
    vSafeHtml.updated!(el, binding('```ts\nconst x = 1;\n```'), vnode, null as never);
    await waitEnhanced();

    expect(el.querySelector('pre.shiki')).not.toBeNull();
  });

  it('copy 动作把代码文本写入剪贴板', async () => {
    const el = document.createElement('div');
    document.body.appendChild(el);
    vSafeHtml.mounted!(el, binding('```ts\nconst x = 1;\n```'), vnode, null as never);
    await waitEnhanced();

    (el.querySelector('[data-act="copy"]') as HTMLElement).click();
    await vi.waitFor(() => {
      expect(vi.mocked(navigator.clipboard.writeText).mock.calls.length).toBeGreaterThan(0);
    });
    expect(String(vi.mocked(navigator.clipboard.writeText).mock.calls[0]![0])).toContain('const x = 1;');
  });

  it('未知语言 fail-open：原块保留（不崩不替换）', async () => {
    const el = document.createElement('div');
    document.body.appendChild(el);
    vSafeHtml.mounted!(el, binding('```qqqzz\nplain\n```'), vnode, null as never);
    await new Promise(resolve => setTimeout(resolve, 800));

    expect(el.querySelector('pre.shiki')).toBeNull();
    expect(el.querySelector('pre code')?.textContent).toContain('plain');
  });

  it('mermaid fence 渲染为 figure（mock mermaid），坏图保留代码', async () => {
    mermaid.render.mockResolvedValueOnce({ svg: '<svg><g>diagram</g></svg>' });
    const el = document.createElement('div');
    document.body.appendChild(el);
    vSafeHtml.mounted!(el, binding('```mermaid\ngraph TD; A-->B;\n```'), vnode, null as never);
    await vi.waitFor(() => {
      if (!el.querySelector('.mermaid-figure svg')) throw new Error('pending');
    });
    expect(el.querySelector('.mermaid-figure [data-act="zoom"]')).not.toBeNull();
    // HTML labels land in <foreignObject>, which the SVG sanitizer strips;
    // text labels keep the node captions visible.
    expect(mermaid.initialize).toHaveBeenCalledWith(expect.objectContaining({ htmlLabels: false }));

    mermaid.render.mockRejectedValueOnce(new Error('bad syntax'));
    const el2 = document.createElement('div');
    document.body.appendChild(el2);
    vSafeHtml.mounted!(el2, binding('```mermaid\nbroken\n```'), vnode, null as never);
    await new Promise(resolve => setTimeout(resolve, 600));
    expect(el2.querySelector('.mermaid-figure')).toBeNull();
    expect(el2.querySelector('pre code')?.textContent).toContain('broken');
  });

  it('KaTeX：注册后 $…$ 渲染为公式（真 katex）', async () => {
    vi.mock('../../composables/use-katex', async importOriginal => {
      const actual = await importOriginal<typeof import('../../composables/use-katex')>();
      return actual;
    });
    const el = document.createElement('div');
    document.body.appendChild(el);
    vSafeHtml.mounted!(el, binding('欧拉公式 $e^{i\\pi}+1=0$ 与文字'), vnode, null as never);
    // katex lands asynchronously (lazy registration) — allow the import to finish.
    await vi.waitFor(
      () => {
        if (!el.querySelector('.katex')) throw new Error('katex not registered yet');
      },
      { timeout: 8000, interval: 100 }
    );
    const formula = el.querySelector<HTMLElement>('.katex');
    expect(formula).not.toBeNull();
    expect(formula!.dataset.act).toBe('zoom');
    expect(formula!.getAttribute('tabindex')).toBe('0');

    formulaPreview.open.mockClear();
    formula!.click();
    await vi.waitFor(() => expect(formulaPreview.open).toHaveBeenCalledTimes(1));
    expect(String(formulaPreview.open.mock.calls[0]![0])).toContain('katex');
  });

  it('resolveShikiLang 的别名与未知语言', () => {
    expect(resolveShikiLang('ts')).toBe('typescript');
    expect(resolveShikiLang('py')).toBe('python');
    expect(resolveShikiLang('bash')).toBe('shell');
    expect(resolveShikiLang('qqqzz')).toBeNull();
  });

  it('safeMarkdownHtml 输出的 fence 仍是增强入口的形状', () => {
    const html = safeMarkdownHtml('```js\nlet a = 1;\n```');
    expect(html).toContain('<pre><code class="language-js">');
  });
});
