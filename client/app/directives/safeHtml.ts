import type { DirectiveBinding, ObjectDirective } from 'vue';
import DOMPurify from 'dompurify';
import MarkdownIt from 'markdown-it';
import { chatPurifyConfig } from '@/constants/security';
import { logUtil } from '~/utils/log';

/**
 * v-safe-html —— 安全渲染指令：markdown → HTML → DOMPurify 白名单净化。
 *
 * 替代裸 `v-html`，把「渲染 + 净化」收敛到唯一入口（配合 ESLint `vue/no-v-html: error`，
 * 项目内所有原始 HTML 渲染都必须经过本指令）。
 *
 * 净化策略见 `app/constants/security.ts`（chatPurifyConfig）。
 *
 * 用法：
 *   <div v-safe-html="message.content"></div>
 *
 * 流式渲染说明：
 *   AI 回复逐块追加时 message.content 持续变化，Vue 会在绑定值变化时调用
 *   `updated` 钩子重新渲染/净化；绑定值未变化时不会重复净化（相比原先
 *   在 render 中对每条消息调用 safeHtml() 的写法，减少了无谓的重复计算）。
 */

/** markdown-it 实例（模块级单例）：html: true 透传原始 HTML（随后交给 DOMPurify 白名单把关），linkify 自动识别裸链接 */
const md = new MarkdownIt({ html: true, linkify: true });

// 净化后钩子：为所有 <a> 补写 rel="noopener noreferrer"，
// 防止链接在新上下文打开时拿到 window.opener 引用（反向标签劫持）。
// afterSanitizeAttributes 在属性校验之后执行，此处写入不会被再次剥离。
DOMPurify.addHook('afterSanitizeAttributes', node => {
  const el = node as Element | null;
  if (el && el.tagName === 'A') {
    el.setAttribute('rel', 'noopener noreferrer');
  }
});

/**
 * 将 markdown 文本渲染为经白名单净化的安全 HTML 片段。
 *
 * @param content 原始 markdown 文本（消息正文）
 * @returns 可安全写入 innerHTML 的 HTML；输入为空或净化异常时返回空串（fail-safe）
 */
export function safeMarkdownHtml(content: string | null | undefined): string {
  if (!content) return '';
  try {
    return DOMPurify.sanitize(md.render(content), chatPurifyConfig);
  } catch (error) {
    // 净化环节任何异常都按「不可信」处理：宁可丢弃正文也不输出未净化内容
    logUtil.e('[v-safe-html] sanitize failed, fallback to empty html:', error);
    return '';
  }
}

/** 指令实现：mounted 与 updated 共用同一渲染路径，保证流式追加时内容实时更新 */
const render = (el: HTMLElement, binding: DirectiveBinding<string | null | undefined>): void => {
  el.innerHTML = safeMarkdownHtml(binding.value);
};

export const vSafeHtml: ObjectDirective<HTMLElement, string | null | undefined> = {
  mounted: render,
  updated: render
};

export default vSafeHtml;
