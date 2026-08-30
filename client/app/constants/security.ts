import type { Config } from 'dompurify';

/**
 * DOMPurify 统一净化配置 —— 聊天消息 markdown 渲染专用（v-safe-html 指令）。
 *
 * 设计要点：
 *  - 白名单 = markdown-it 核心（v15，默认 preset、无插件）的完整输出标签集：
 *    块级（p/blockquote/hr/ul/ol/li/pre/code/标题/表格）+ 行内（strong/em/s/del/a/img/br/span）。
 *    本项目未启用语法高亮（shiki/hljs）、公式（KaTeX）或任务列表插件，
 *    因此无需为它们放行标签；原始 HTML 中不在白名单的标签（div/section/video…）
 *    会被整体移除（文本内容保留），这是刻意的收窄。
 *  - 片段模式：md.render() 输出的是 HTML 片段而非完整文档，
 *    绝不能开启 WHOLE_DOCUMENT（会把输出包上 <html><body> 破坏片段语义）。
 *  - style 属性必须放行：markdown-it 的 GFM 表格对齐通过
 *    th/td 上的 style="text-align:…" 实现；属性值本身仍由 DOMPurify 净化。
 *  - ALLOW_DATA_ATTR: false —— 原始 HTML 里的 data-* 一律剥离。
 */
export const chatPurifyConfig: Config = {
  WHOLE_DOCUMENT: false,
  ALLOWED_TAGS: [
    // 块级
    'p',
    'br',
    'hr',
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
    'blockquote',
    'pre',
    'code',
    'ul',
    'ol',
    'li',
    'table',
    'thead',
    'tbody',
    'tr',
    'th',
    'td',
    // 行内
    'strong',
    'em',
    's',
    'del',
    'span',
    'a',
    'img'
  ],
  ALLOWED_ATTR: [
    // a
    'href',
    'title',
    // img
    'src',
    'alt',
    // code（fence 的 language-* class）、span 等
    'class',
    // GFM 表格对齐（th/td 的 text-align）
    'style',
    // v-safe-html 钩子为外链补写的 rel="noopener noreferrer"
    'rel'
  ],
  ALLOW_DATA_ATTR: false
};
