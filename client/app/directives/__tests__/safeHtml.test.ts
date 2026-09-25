// @vitest-environment jsdom
/*
 * This suite must run under jsdom rather than the project default of happy-dom:
 * the DOMPurify README explicitly warns "Combining DOMPurify with happy-dom is currently
 * not recommended and will likely lead to XSS" — happy-dom's DOM implementation distorts
 * the sanitization result (the fragment's first element gets swallowed, sibling nodes escape
 * sanitization), so it cannot be used to verify real browser (WebView2/Chromium) security
 * behavior. The project's other test suites remain on happy-dom, unaffected.
 */
import { describe, it, expect } from 'vitest';
import { createVNode } from 'vue';
import type { ComponentPublicInstance, DirectiveBinding, VNode } from 'vue';
import { vSafeHtml, safeMarkdownHtml } from '../safeHtml';

/**
 * Constructs a minimal usable DirectiveBinding (for tests)
 * @param value
 */
const makeBinding = (value: string | null | undefined): DirectiveBinding<string | null | undefined> => ({
  // No real component instance exists in unit tests; the hooks only read
  // (el, binding), so an empty stub fills the required `instance` slot.
  instance: {} as ComponentPublicInstance,
  value,
  oldValue: null,
  arg: undefined,
  modifiers: {},
  dir: vSafeHtml
});

/**
 * Shared vnode/prevVnode placeholder for hook invocation (the hooks only read (el, binding)).
 * createVNode's public API hardcodes the renderer-generic pair (VNode<RendererNode, RendererElement>)
 * which is not directly assignable to the HTMLElement-bound hook signature, so the stub is
 * adapted once here; the runtime shape is a plain element vnode, exactly what Vue passes.
 */
const vnode = createVNode('div') as VNode<any, HTMLElement>;

describe('safeMarkdownHtml — XSS 攻击向量', () => {
  it('剥离 <script> 标签，保留正文文本', () => {
    const html = safeMarkdownHtml('<script>alert(1)</script>hi');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('</script>');
    expect(html).toContain('hi');
  });

  it('剥离 on* 事件属性（img onerror）', () => {
    const html = safeMarkdownHtml('<img src=x onerror="alert(1)">');
    expect(html).not.toContain('onerror');
    expect(html.toLowerCase()).not.toContain('alert(1)');
  });

  it('剥离 javascript: 伪协议链接', () => {
    const html = safeMarkdownHtml('<a href="javascript:alert(1)">点我</a>');
    expect(html).not.toContain('javascript:');
    expect(html).toContain('点我'); // the tag is whitelisted; only the href was removed
  });

  it('剥离白名单外的危险标签（svg/iframe/object）', () => {
    const html = safeMarkdownHtml(
      '<svg onload="alert(1)"></svg><iframe src="https://evil.example"></iframe><object data="x"></object>正文'
    );
    expect(html.toLowerCase()).not.toContain('<svg');
    expect(html.toLowerCase()).not.toContain('<iframe');
    expect(html.toLowerCase()).not.toContain('<object');
    expect(html).toContain('正文');
  });

  it('剥离白名单外标签上的事件属性（div onclick），保留其文本', () => {
    const html = safeMarkdownHtml('<div onclick="alert(1)">内容</div>');
    expect(html).not.toContain('onclick');
    expect(html).not.toContain('<div');
    expect(html).toContain('内容');
  });

  it('剥离 data-* 属性（ALLOW_DATA_ATTR: false）', () => {
    const html = safeMarkdownHtml('<a href="https://example.com" data-payload="x">链接</a>');
    expect(html).not.toContain('data-payload');
  });
});

describe('safeMarkdownHtml — 合法 markdown 渲染不被误伤', () => {
  it('加粗/标题正常渲染', () => {
    expect(safeMarkdownHtml('**bold**')).toContain('<strong>bold</strong>');
    expect(safeMarkdownHtml('# title')).toContain('<h1>title</h1>');
  });

  it('代码围栏保留 language-* class', () => {
    const html = safeMarkdownHtml('```js\nconsole.log(1)\n```');
    expect(html).toContain('<pre><code class="language-js">');
  });

  it('linkify 链接保留 href，并由钩子补写 rel="noopener noreferrer"', () => {
    const html = safeMarkdownHtml('visit https://example.com');
    expect(html).toContain('<a href="https://example.com"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it('GFM 表格对齐 style 属性被保留', () => {
    const md = '| a | b |\n|:-:|--:|\n| 1 | 2 |';
    const html = safeMarkdownHtml(md);
    expect(html).toContain('text-align:center');
    expect(html).toContain('text-align:right');
  });

  it('style 值走 URL 门：非对齐声明与拼接尾巴整值拒绝', () => {
    // 必须用真实表格上下文：裸 <td> 会被 HTML 解析器整体丢弃，测不到 style 门
    const color = safeMarkdownHtml('<table><tr><th style="color:red">h</th></tr></table>');
    expect(color).not.toContain('color');

    // 整值匹配：text-align 前缀合法 + 尾部拼 url() 必须整值拒绝，而非前缀保留
    const appended = safeMarkdownHtml(
      '<table><tr><th style="text-align:center;background:url(https://evil.example/x)">h</th></tr></table>'
    );
    expect(appended).not.toContain('url(');
    expect(appended).not.toContain('text-align');
  });

  it('URL 门：javascript:/data: 的 href 被剥除；img 的 data: 属 DOMPurify 媒体豁免', () => {
    const html = safeMarkdownHtml(
      '<a href="javascript:alert(1)">x</a>' +
        '<a href="data:text/html,evil">y</a>' +
        '<img src="data:image/png;base64,AAAA" alt="ok">'
    );
    expect(html).not.toContain('javascript:');
    // <a> 不在 DATA_URI_TAGS 豁免名单内，data: href 必须走 URI 门被剥除
    expect(html).not.toContain('href="data:');
    // DOMPurify 对媒体标签（img 等）的 data: src 有专门的存活豁免（惰性媒体，
    // 非注入向量）——钉死为文档化行为，防止误判为泄漏
    expect(html).toContain('src="data:image/png');
    expect(html).toContain('x');
  });

  it('白名单外的行内标签（如 <b>）被移除但文本保留', () => {
    const html = safeMarkdownHtml('前 <b>加粗</b> 后');
    expect(html).not.toContain('<b>');
    expect(html).toContain('加粗');
  });

  it('空输入返回空串（fail-safe）', () => {
    expect(safeMarkdownHtml('')).toBe('');
    expect(safeMarkdownHtml(null)).toBe('');
    expect(safeMarkdownHtml(undefined)).toBe('');
  });
});

describe('vSafeHtml 指令', () => {
  it('mounted 渲染净化后的 markdown', () => {
    const el = document.createElement('div');
    vSafeHtml.mounted!(el, makeBinding('**bold**'), vnode, null);
    expect(el.innerHTML).toContain('<strong>bold</strong>');
  });

  it('updated 随绑定值变化重新渲染（流式追加场景）', () => {
    const el = document.createElement('div');
    vSafeHtml.mounted!(el, makeBinding('first'), vnode, null);
    expect(el.innerHTML).toContain('first');
    vSafeHtml.updated!(el, makeBinding('# second'), vnode, vnode);
    expect(el.innerHTML).toContain('<h1>second</h1>');
    expect(el.innerHTML).not.toContain('first');
  });

  it('updated 传入空值时清空内容', () => {
    const el = document.createElement('div');
    vSafeHtml.mounted!(el, makeBinding('**bold**'), vnode, null);
    vSafeHtml.updated!(el, makeBinding(''), vnode, vnode);
    expect(el.innerHTML).toBe('');
  });

  it('mounted 对 XSS 载荷净化为空/安全内容', () => {
    const el = document.createElement('div');
    vSafeHtml.mounted!(el, makeBinding('<script>alert(1)</script>hi'), vnode, null);
    expect(el.querySelector('script')).toBeNull();
    expect(el.textContent).toContain('hi');
  });
});
