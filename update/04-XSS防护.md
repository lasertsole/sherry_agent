# XSS 防护体系 — 可复用实现指南

> 本文档从 RetailMiniAPP 和 CarMiniApp 两个项目中提炼，提供了三层 XSS 防护方案：DOMPurify 净化器、xss 库富文本过滤器、纯原生白名单 HTML 指令，可按需组合使用到任意前端项目中。

---

## 目录

1. [XSS 防护总览](#1-xss-防护总览)
2. [方案一：DOMPurify 统一净化](#2-方案一dompurify-统一净化)
3. [方案二：xss 库富文本过滤器](#3-方案二xss-库富文本过滤器)
4. [方案三：纯原生白名单 HTML 指令](#4-方案三纯原生白名单-html-指令)
5. [Vue 指令封装](#5-vue-指令封装)
6. [React 适配方案](#6-react-适配方案)
7. [防护方案对比与选型](#7-防护方案对比与选型)

---

## 1. XSS 防护总览

### 1.1 常见 XSS 攻击向量

| 攻击类型           | 示例 payload                                           | 危害              |
| ------------------ | ------------------------------------------------------ | ----------------- |
| `<script>` 注入    | `<script>alert(document.cookie)</script>`              | 执行任意 JS       |
| 事件属性注入       | `<img src=x onerror="alert(1)">`                       | 加载失败时执行 JS |
| `javascript:` 协议 | `<a href="javascript:alert(1)">`                       | 点击链接执行 JS   |
| `data:` URI        | `<img src="data:text/html,<script>alert(1)</script>">` | 内嵌恶意 HTML     |
| SVG 内嵌脚本       | `<svg onload="alert(1)">`                              | 加载时执行 JS     |
| CSS 表达式         | `<div style="background:url('javascript:alert(1)')">`  | 旧浏览器执行 JS   |

### 1.2 三层防护架构

```
                    ┌─────────────────────────┐
                    │   用户输入 / API 返回值   │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
  第一层：净化      │  DOMPurify.sanitize()    │  ← 通用 HTML 净化
                    │  或 richTextFilter()     │  ← 富文本精细过滤
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
  第二层：渲染      │  v-safe-html 指令        │  ← 替代 v-html
                    │  或 dangerouslySetInnerHTML(净化后) │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
  第三层：规则      │  ESLint: vue/no-v-html:2 │  ← 禁止 v-html
                    │  CSP 策略                │  ← 内容安全策略
                    └─────────────────────────┘
```

---

## 2. 方案一：DOMPurify 统一净化

### 2.1 安装

```bash
npm install dompurify
npm install -D @types/dompurify
```

### 2.2 统一配置

将 DOMPurify 的配置集中管理，确保全项目使用一致的净化策略：

```typescript
// src/constants/security.ts
import { Config } from "dompurify";

/**
 * DOMPurify.sanitize 方法的统一配置
 *
 * WHOLE_DOCUMENT: true — 将输入视为完整 HTML 文档进行净化，
 *   能更有效地检测和移除 `<html>`、`<head>`、`<body>` 标签外的恶意内容
 */
export const purifyConfig: Config = {
  WHOLE_DOCUMENT: true,
};

/**
 * 富文本场景的 DOMPurify 配置（允许更多标签）
 */
export const richTextPurifyConfig: Config = {
  WHOLE_DOCUMENT: false,
  ALLOWED_TAGS: [
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "img",
    "ul",
    "ol",
    "li",
    "table",
    "tr",
    "td",
    "th",
    "thead",
    "tbody",
    "strong",
    "em",
    "u",
    "s",
    "del",
    "blockquote",
    "pre",
    "code",
    "br",
    "hr",
    "span",
    "div",
  ],
  ALLOWED_ATTR: [
    "href",
    "title",
    "target",
    "rel",
    "src",
    "alt",
    "width",
    "height",
    "colspan",
    "rowspan",
    "style",
    "class",
  ],
  ALLOW_DATA_ATTR: false,
};

/**
 * 纯文本场景的 DOMPurify 配置（移除所有 HTML 标签）
 */
export const textOnlyPurifyConfig: Config = {
  ALLOWED_TAGS: [], // 不允许任何标签
  ALLOWED_ATTR: [], // 不允许任何属性
};
```

### 2.3 使用方式

```typescript
import DOMPurify from "dompurify";
import {
  purifyConfig,
  richTextPurifyConfig,
  textOnlyPurifyConfig,
} from "@/constants/security";

// 通用净化
const cleanHtml = DOMPurify.sanitize(dirtyHtml, purifyConfig);

// 富文本净化
const cleanRichText = DOMPurify.sanitize(userContent, richTextPurifyConfig);

// 纯文本净化（移除所有 HTML）
const cleanText = DOMPurify.sanitize(input, textOnlyPurifyConfig);

// 在 Vue 组件中使用
// <div v-html="sanitizedHtml"></div>
const sanitizedHtml = computed(() => {
  return DOMPurify.sanitize(rawHtml.value, richTextPurifyConfig);
});
```

### 2.4 进阶：自定义 DOMPurify 钩子

```typescript
import DOMPurify from "dompurify";

// 在净化后给所有 <a> 标签添加 target="_blank" 和 rel="noopener noreferrer"
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

// 在净化前移除所有 style 属性中的 expression()
DOMPurify.addHook("uponSanitizeAttribute", (node, data) => {
  if (data.attrName === "style") {
    data.attrValue = data.attrValue.replace(/expression\s*\(/gi, "");
  }
});
```

---

## 3. 方案二：xss 库富文本过滤器

### 3.1 安装

```bash
npm install xss
```

### 3.2 完整实现

````typescript
// src/utils/richTextFilter.ts
import * as xss from "xss";

// ============ 类型定义 ============

interface RichTextFilterOptions extends xss.IFilterXSSOptions {
  /** 允许的标签列表 */
  allowedTags: string[];
  /** 每个标签允许的属性 */
  allowedAttributes: Record<string, string[]>;
  /** 允许的内联样式 */
  allowedStyles?: Record<string, Record<string, RegExp[]>>;
}

// ============ HTML 字符串校验 ============

/**
 * 检查输入是否是有效的 HTML 字符串
 */
function isHtmlString(input: unknown): input is string {
  return typeof input === "string" && /<[a-z][\s\S]*>/i.test(input);
}

// ============ 默认白名单配置 ============

const defaultOptions: RichTextFilterOptions = {
  // 允许的标签
  allowedTags: [
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "img",
    "ul",
    "ol",
    "li",
    "table",
    "tr",
    "td",
    "th",
    "strong",
    "em",
    "u",
    "s",
    "blockquote",
    "pre",
    "code",
  ],

  // 每个标签允许的属性
  allowedAttributes: {
    a: ["href", "title", "target", "rel"],
    img: ["src", "alt", "width", "height", "style"],
    table: ["border", "cellpadding", "cellspacing"],
    td: ["colspan", "rowspan"],
    th: ["colspan", "rowspan"],
  },

  // 允许的内联样式（使用正则白名单）
  allowedStyles: {
    "*": {
      "text-align": [/^left$/, /^right$/, /^center$/],
      color: [/^#(0x)?[0-9a-f]+$/i],
      "font-size": [/^\d+(?:px|em|%)$/],
    },
  },
};

// ============ 核心过滤函数 ============

/**
 * 富文本 XSS 过滤器
 *
 * 基于 xss 库的 FilterXSS，通过白名单机制过滤 HTML
 *
 * @param dirtyHtml 待过滤的 HTML 字符串
 * @param customOptions 可选的自定义过滤选项（与默认配置深度合并）
 * @returns 过滤后的安全 HTML 字符串
 *
 * @example
 * ```typescript
 * const clean = richTextFilter('<p>Hello <script>alert(1)</script>World</p>');
 * // 结果: '<p>Hello World</p>'
 *
 * const clean2 = richTextFilter(html, {
 *   allowedTags: [...defaultOptions.allowedTags, 'div', 'span'],
 *   allowedAttributes: {
 *     ...defaultOptions.allowedAttributes,
 *     div: ['class'],
 *     span: ['class', 'style']
 *   }
 * });
 * ```
 */
export function richTextFilter(
  dirtyHtml: string,
  customOptions?: Partial<RichTextFilterOptions>,
): string {
  // 深度合并配置（allowedAttributes 和 allowedStyles 是对象，需要单独合并）
  const options: RichTextFilterOptions = {
    ...defaultOptions,
    ...customOptions,
    allowedAttributes: {
      ...defaultOptions.allowedAttributes,
      ...customOptions?.allowedAttributes,
    },
    allowedStyles: {
      ...defaultOptions.allowedStyles,
      ...customOptions?.allowedStyles,
    },
  };

  // 创建 xss 过滤器实例
  const xssInstance = new xss.FilterXSS(options);

  try {
    if (typeof dirtyHtml !== "string") {
      throw new TypeError("输入必须是字符串类型");
    }

    return xssInstance.process(dirtyHtml);
  } catch (error) {
    console.error("富文本过滤错误:", error);
    // fail-safe：出错时返回空字符串
    return xssInstance.process("");
  }
}

// ============ 便捷方法 ============

/**
 * 纯文本过滤（移除所有 HTML 标签）
 */
export function plainTextFilter(input: string): string {
  const xssInstance = new xss.FilterXSS({
    whiteList: {}, // 不允许任何标签
    stripIgnoreTag: true, // 移除不在白名单中的标签
    stripIgnoreTagBody: ["script", "style"], // script/style 标签内容也移除
  });

  return xssInstance.process(input);
}
````

### 3.3 使用方式

```typescript
import { richTextFilter, plainTextFilter } from "@/utils/richTextFilter";

// 基本用法
const dirty =
  '<p>Hello <script>alert(1)</script><img src=x onerror="alert(1)"> World</p>';
const clean = richTextFilter(dirty);
// 结果: '<p>Hello <img src="x" alt=""> World</p>'
// script 标签被移除，onerror 属性被移除

// 纯文本
const text = plainTextFilter("<p>Hello <b>World</b></p>");
// 结果: 'Hello World'

// 自定义白名单
const clean2 = richTextFilter(html, {
  allowedTags: ["p", "div", "span", "a"],
  allowedAttributes: {
    a: ["href", "target"],
    div: ["class"],
    span: ["class"],
  },
});
```

### 3.4 xss 库白名单机制说明

xss 库（`xss` npm 包）的工作原理：

```
输入 HTML
  │
  ▼
FilterXSS.parseHtml()  — 使用内置 HTML 解析器解析为 AST
  │
  ▼
遍历 AST 节点
  │
  ├─ 标签节点
  │    ├─ 在 whiteList / allowedTags 中？ → 保留
  │    │    └─ 遍历属性
  │    │         ├─ 在 allowedAttributes 中？ → 保留
  │    │         ├─ on* 事件属性？ → 移除（内置规则）
  │    │         └─ style 属性？ → 用 allowedStyles 正则校验
  │    └─ 不在白名单中？ → 标签移除（内容可选保留）
  │
  └─ 文本节点 → 直接保留（HTML 实体会被编码）
  │
  ▼
输出安全 HTML
```

---

## 4. 方案三：纯原生白名单 HTML 指令

> 此方案无需任何第三方依赖，纯原生 DOM API 实现。完整实现见 `02-Vue自定义指令.md` 中的 `v-safe-html` 部分，此处补充关键设计说明。

### 4.1 核心设计

```typescript
// 不使用第三方库，纯原生 DOM API
// 1. document.createDocumentFragment() — 创建文档片段（不触发回流）
// 2. 逐字符状态机解析 HTML — 完全控制解析过程
// 3. document.createElement() — 只创建白名单中的标签
// 4. element.setAttribute() — 只设置白名单中的属性
// 5. 自动过滤：on* 事件属性、javascript: 协议、<script> 标签
```

### 4.2 与 v-html 的对比

| 维度     | v-html               | v-safe-html            |
| -------- | -------------------- | ---------------------- |
| 安全性   | 无过滤，XSS 高风险   | 白名单过滤，XSS 安全   |
| 性能     | 浏览器原生 innerHTML | 手动解析，略慢但可接受 |
| 依赖     | 无                   | 无                     |
| 标签支持 | 全部                 | 仅白名单               |
| 属性支持 | 全部                 | 仅白名单               |

### 4.3 使用方式

```vue
<template>
  <!-- 替代 v-html -->
  <div v-safe-html="richText"></div>
</template>

<script setup lang="ts">
import { vSafeHtml } from "@/directives/safeHtml";
// vSafeHtml 自动注册为 v-safe-html
</script>
```

---

## 5. Vue 指令封装

### 5.1 统一安全渲染指令（结合 DOMPurify + 原生白名单）

```typescript
// src/directives/safeRender.ts
import type { Directive, DirectiveBinding } from "vue";
import DOMPurify from "dompurify";
import { purifyConfig } from "@/constants/security";

interface SafeRenderConfig {
  /** 使用哪个净化引擎：dompurify / whitelist */
  engine?: "dompurify" | "whitelist";
  /** DOMPurify 自定义配置 */
  purifyOptions?: Config;
}

type SafeRenderValue = string | { html: string; config?: SafeRenderConfig };

// 默认使用 DOMPurify 引擎
export const vSafeRender: Directive<HTMLElement, SafeRenderValue> = {
  mounted(el: HTMLElement, binding: DirectiveBinding) {
    renderSafe(el, binding.value);
  },
  updated(el: HTMLElement, binding: DirectiveBinding) {
    renderSafe(el, binding.value);
  },
};

function renderSafe(el: HTMLElement, value: SafeRenderValue): void {
  // 清空
  while (el.firstChild) {
    el.removeChild(el.firstChild);
  }

  let html = "";
  let config: SafeRenderConfig = { engine: "dompurify" };

  if (typeof value === "string") {
    html = value;
  } else if (value && typeof value === "object") {
    html = value.html;
    config = { engine: "dompurify", ...value.config };
  } else {
    return;
  }

  if (!html) return;

  if (config.engine === "dompurify") {
    // 使用 DOMPurify 净化后设置 innerHTML
    const clean = DOMPurify.sanitize(
      html,
      config.purifyOptions || purifyConfig,
    );
    el.innerHTML = clean;
  } else {
    // 使用纯原生白名单解析（见 02-Vue自定义指令.md 中的 parseHtmlToFragment）
    // const fragment = parseHtmlToFragment(html);
    // el.appendChild(fragment);
  }
}
```

### 5.2 使用

```vue
<template>
  <!-- 默认使用 DOMPurify -->
  <div v-safe-render="userContent"></div>

  <!-- 指定引擎 -->
  <div
    v-safe-render="{ html: userContent, config: { engine: 'whitelist' } }"
  ></div>
</template>

<script setup lang="ts">
import { vSafeRender } from "@/directives/safeRender";
</script>
```

---

## 6. React 适配方案

### 6.1 DOMPurify + dangerouslySetInnerHTML

```tsx
import DOMPurify from "dompurify";
import { purifyConfig } from "@/constants/security";

function SafeHtml({ html }: { html: string }) {
  const clean = DOMPurify.sanitize(html, purifyConfig);
  return <div dangerouslySetInnerHTML={{ __html: clean }} />;
}

// 使用
function App() {
  return <SafeHtml html={apiResponse.richText} />;
}
```

### 6.2 自定义 Hook

```tsx
import { useMemo } from "react";
import DOMPurify from "dompurify";
import { richTextPurifyConfig } from "@/constants/security";

function useSanitizedHtml(dirtyHtml: string, config = richTextPurifyConfig) {
  return useMemo(() => {
    try {
      return DOMPurify.sanitize(dirtyHtml, config);
    } catch (error) {
      console.error("Sanitization failed:", error);
      return "";
    }
  }, [dirtyHtml, config]);
}

// 使用
function RichTextContent({ content }: { content: string }) {
  const sanitized = useSanitizedHtml(content);
  return <div dangerouslySetInnerHTML={{ __html: sanitized }} />;
}
```

### 6.3 xss 库过滤器（React 版）

```tsx
import { richTextFilter } from "@/utils/richTextFilter";

function FilteredContent({ html }: { html: string }) {
  const clean = richTextFilter(html);
  return <div dangerouslySetInnerHTML={{ __html: clean }} />;
}
```

---

## 7. 防护方案对比与选型

### 7.1 三种方案对比

| 维度           | DOMPurify         | xss 库                  | 纯原生白名单指令         |
| -------------- | ----------------- | ----------------------- | ------------------------ |
| **安全性**     | 极高（业界标准）  | 高                      | 高（取决于白名单完整性） |
| **包体积**     | ~45KB (minified)  | ~28KB (minified)        | 0（无依赖）              |
| **性能**       | 快（C 级优化）    | 快                      | 中等（手动解析）         |
| **可配置性**   | 高（钩子 + 配置） | 高（白名单 + 样式正则） | 中（白名单）             |
| **维护性**     | 高（社区活跃）    | 中（较少更新）          | 低（需自己维护解析器）   |
| **TypeScript** | 有类型定义        | 有类型定义              | 原生 TS                  |
| **适用场景**   | 通用 HTML 净化    | 富文本编辑器内容        | 轻量需求 / 无依赖要求    |

### 7.2 选型建议

| 场景                       | 推荐方案                          | 原因                           |
| -------------------------- | --------------------------------- | ------------------------------ |
| 通用项目（有 v-html 需求） | DOMPurify + v-safe-render 指令    | 业界标准，安全性最高           |
| 富文本编辑器内容渲染       | xss 库 richTextFilter             | 精细的标签/属性/样式白名单控制 |
| 极度轻量项目（不想加依赖） | 纯原生 v-safe-html 指令           | 零依赖，白名单够用             |
| React 项目                 | DOMPurify + useSanitizedHtml Hook | React 生态兼容好               |
| 需要双重保险               | DOMPurify + xss 库组合使用        | 两层过滤，极致安全             |

### 7.3 RetailMiniAPP 项目实际使用方式

```
RetailMiniAPP 的 XSS 防护策略：
├── dompurify (3.4.12)  — 通用 HTML 净化，配置集中在 constants/security.ts
├── xss (1.0.15)        — 富文本精细过滤，实现在 utils/richTextFilter.ts
├── ESLint 规则          — vue/no-v-html: 2 禁止使用 v-html
└── CarMiniApp 补充      — v-safe-html 指令（纯原生白名单，零依赖）
```

---

## 8. ESLint 规则配合

在 ESLint 配置中强制禁止 `v-html`，确保所有 HTML 渲染都走安全通道：

```javascript
// eslint.config.mjs
{
  rules: {
    // 禁止 v-html（Vue 项目）
    'vue/no-v-html': 2,

    // React 项目等效：禁止 dangerouslySetInnerHTML（需 eslint-plugin-react）
    // 'react/no-danger': 2,
    // 'react/no-danger-with-children': 2,
  }
}
```

---

## 9. CSP（内容安全策略）配合

在 HTML 或服务器响应头中配置 CSP，作为 XSS 防护的最后一道防线：

```html
<!-- index.html -->
<meta
  http-equiv="Content-Security-Policy"
  content="
    default-src 'self';
    script-src 'self' 'unsafe-inline';
    style-src 'self' 'unsafe-inline';
    img-src 'self' data: https:;
    connect-src 'self' https://api.example.com;
  "
/>

<!-- 或在服务器/Nginx 配置响应头 -->
<!-- add_header Content-Security-Policy "default-src 'self'; script-src 'self';" always; -->
```

CSP 即使在 XSS 过滤被绕过时，也能阻止内联脚本执行。

---

## 10. 快速接入检查清单

- [ ] 安装 `dompurify` 和/或 `xss` 库
- [ ] 创建 `src/constants/security.ts`，配置 DOMPurify 统一配置
- [ ] 创建 `src/utils/richTextFilter.ts`，实现富文本过滤器
- [ ] 创建 `src/directives/safeHtml.ts` 或 `safeRender.ts` 指令
- [ ] 全局注册指令（或使用 `<script setup>` 自动注册）
- [ ] 在 ESLint 配置中启用 `vue/no-v-html: 2` 禁止 v-html
- [ ] 搜索项目中所有 `v-html` 使用点，替换为 `v-safe-html` 或 `v-safe-render`
- [ ] 编写 XSS 攻击测试用例，验证过滤效果
- [ ] 可选：配置 CSP 策略作为最后防线
- [ ] 可选：添加 DOMPurify 钩子（如自动给 `<a>` 添加 `rel="noopener"`）
