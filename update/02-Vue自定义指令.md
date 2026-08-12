# Vue 自定义指令 — 可复用实现指南

> 本文档从 RetailMiniAPP 和 CarMiniApp 两个 Vue 3 项目中提炼，提供了防抖指令（v-debounce）和安全 HTML 指令（v-safe-html）的完整实现，可直接复制到任意 Vue 3 项目中使用。

---

## 目录

1. [防抖指令 v-debounce](#1-防抖指令-v-debounce)
2. [安全 HTML 指令 v-safe-html](#2-安全-html-指令-v-safe-html)
3. [全局注册方式](#3-全局注册方式)
4. [异构项目适配指南](#4-异构项目适配指南)

---

## 1. 防抖指令 v-debounce

### 1.1 解决什么问题

按钮频繁点击导致重复提交、搜索输入框频繁触发请求、滚动事件频繁回调。传统方案是在每个函数里手写 debounce 逻辑，指令方案是在模板上声明式添加防抖，零侵入。

### 1.2 功能特性

- 支持任意 DOM 事件类型（click / input / scroll / blur / ...）
- 支持自定义延迟时间（默认 500ms）
- 支持立即执行模式（首次触发立即执行，后续防抖）
- 支持函数简写和对象配置两种写法
- 支持同一元素绑定多个事件类型
- 组件卸载时自动清理事件监听和定时器

### 1.3 完整实现

```typescript
// src/directives/debounce.ts
import { DirectiveBinding, ObjectDirective, VNode } from "vue";

// ============ 类型定义 ============

interface DebounceBinding {
  /** 需要防抖的回调函数 */
  handler: (...args: unknown[]) => void;
  /** 延迟毫秒数，默认 500ms */
  delay?: number;
  /** 是否立即执行（先执行后等待），默认 false（先等待后执行） */
  immediate?: boolean;
}

interface IDebounceOptions {
  delay: number;
  immediate: boolean;
}

/** 扩展 HTMLElement，存储防抖相关状态 */
interface DebounceElement extends HTMLElement {
  _debounce?: {
    originalHandler: (...args: unknown[]) => void;
    debouncedHandler: (event: Event) => void;
    eventType: string;
    timeout: ReturnType<typeof setTimeout> | null;
  } & IDebounceOptions;
}

// ============ 核心逻辑 ============

/**
 * 创建防抖函数并绑定事件监听
 */
function createDebounce(
  el: DebounceElement,
  eventType: string,
  handler: (...args: unknown[]) => void,
  opts: IDebounceOptions,
): void {
  let timeout: ReturnType<typeof setTimeout> | null = null;

  const debouncedHandler = (event: Event) => {
    if (opts.immediate) {
      // 立即执行模式：首次立即调用，后续防抖
      if (timeout) {
        clearTimeout(timeout);
      } else {
        handler(event);
      }
    } else if (timeout) {
      // 延迟执行模式：每次触发都清除并重新计时
      clearTimeout(timeout);
    }

    timeout = setTimeout(() => {
      if (!opts.immediate) {
        handler(event);
      }
      timeout = null;
    }, opts.delay);
  };

  // 将防抖状态存储在 DOM 元素上，供卸载时清理
  el._debounce = {
    originalHandler: handler,
    debouncedHandler,
    eventType,
    timeout,
    delay: opts.delay,
    immediate: opts.immediate,
  };

  el.addEventListener(eventType, debouncedHandler);
}

/**
 * 解析指令绑定值并创建防抖
 */
function bindDebounce(el: DebounceElement, binding: DirectiveBinding): void {
  const { value, arg, modifiers } = binding;

  // 事件类型：通过指令参数指定，如 v-debounce:input，默认 click
  const eventType = arg || "click";

  // 延迟时间：通过修饰符指定，如 v-debounce:click.300，默认 500ms
  let delay = 500;
  const timeModifiers = Object.keys(modifiers)
    .filter((key) => !isNaN(Number(key)))
    .map(Number);
  if (timeModifiers.length > 0) {
    delay = timeModifiers[0];
  }

  let handler: (...args: unknown[]) => void;
  let finalDelay = delay;
  let finalImmediate = false;

  // 支持两种绑定值写法
  if (typeof value === "function") {
    // 简写：v-debounce="handleEvent"
    handler = value;
  } else if (value && typeof value === "object" && "handler" in value) {
    // 对象配置：v-debounce="{ handler: fn, delay: 1000, immediate: true }"
    handler = value.handler;
    finalDelay = value.delay || delay;
    finalImmediate = !!value.immediate;
  } else {
    console.warn("v-debounce 需要绑定函数或包含 handler 的对象");
    return;
  }

  createDebounce(el, eventType, handler, {
    delay: finalDelay,
    immediate: finalImmediate,
  });
}

/**
 * 移除防抖事件监听并清理定时器
 */
function unbindDebounce(el: DebounceElement): void {
  if (el._debounce) {
    clearTimeout(el._debounce.timeout);
    el.removeEventListener(
      el._debounce.eventType,
      el._debounce.debouncedHandler,
    );
    delete el._debounce;
  }
}

// ============ 指令导出 ============

export const vDebounce: ObjectDirective<
  DebounceElement,
  (() => void) | DebounceBinding
> = {
  // 组件挂载时创建防抖并绑定事件
  mounted(el: DebounceElement, binding: DirectiveBinding) {
    bindDebounce(el, binding);
  },

  // 组件更新时（绑定值变化）重新绑定
  updated(el: DebounceElement, binding: DirectiveBinding) {
    unbindDebounce(el);
    bindDebounce(el, binding);
  },

  // 组件卸载前清理
  beforeUnmount(el: DebounceElement) {
    unbindDebounce(el);
  },
};
```

### 1.4 使用示例

```vue
<template>
  <div>
    <!-- 基本用法：click 事件，默认 500ms 延迟 -->
    <button v-debounce="handleClick">提交</button>

    <!-- 指定事件类型 -->
    <button v-debounce:click="handleClick">提交</button>
    <button v-debounce:blur="handleBlur">失焦</button>

    <!-- 指定延迟时间（修饰符） -->
    <button v-debounce:click.200="handleClick">快速防抖 200ms</button>

    <!-- input 搜索防抖 -->
    <input v-debounce:input.300="handleSearch" placeholder="搜索..." />

    <!-- scroll 事件防抖 -->
    <div v-debounce:scroll="handleScroll" style="height: 200px; overflow: auto">
      <!-- 滚动内容 -->
    </div>

    <!-- 对象配置：自定义延迟 + 立即执行 -->
    <button v-debounce="{ handler: handleClick, delay: 1000, immediate: true }">
      立即执行 + 1秒防抖
    </button>

    <!-- 同一元素绑定多个防抖事件 -->
    <button
      v-debounce:click="handleClick"
      v-debounce:blur="{ handler: handleBlur, delay: 300, immediate: true }"
    >
      多事件防抖
    </button>
  </div>
</template>

<script setup lang="ts">
import { vDebounce } from "@/directives/debounce";

// 在 <script setup> 中直接使用 vDebounce 变量名即可
// Vue 3 会自动将 vDebounce 映射为 v-debounce 指令
// （需要 Vue 3.2+ 的自动指令注册功能）

const handleClick = () => {
  console.log("防抖点击");
};

const handleSearch = (event: Event) => {
  const value = (event.target as HTMLInputElement).value;
  console.log("搜索:", value);
};

const handleScroll = () => {
  console.log("滚动防抖");
};

const handleBlur = () => {
  console.log("失焦防抖");
};
</script>
```

### 1.5 原理说明

```
用户点击按钮
  │
  ▼
debouncedHandler(event) 被调用
  │
  ├─ immediate=true 模式：
  │    ├─ timeout 存在？ → clearTimeout（丢弃本次）
  │    └─ timeout 不存在？ → 立即执行 handler，然后设置 timeout
  │         └─ timeout 到期后 → timeout = null（允许下次立即执行）
  │
  └─ immediate=false 模式（默认）：
       ├─ timeout 存在？ → clearTimeout
       └─ 设置新 timeout
            └─ timeout 到期后 → 执行 handler，timeout = null
```

---

## 2. 安全 HTML 指令 v-safe-html

### 2.1 解决什么问题

Vue 的 `v-html` 指令直接将字符串渲染为 DOM，存在 XSS 风险。`v-safe-html` 是 `v-html` 的安全替代方案，通过白名单机制过滤危险标签和属性。

### 2.2 功能特性

- 纯原生 DOM API 解析 HTML（不依赖第三方库）
- 标签白名单：只允许已知安全的标签
- 属性白名单：只允许已知安全的属性
- 自动过滤 `on*` 事件属性（onclick、onerror 等）
- 自动过滤 `javascript:` 危险协议
- 自动过滤 `<script>` 标签
- 支持 HTML 实体解码
- 支持自定义白名单配置
- 解析失败时清空内容（fail-safe）

### 2.3 完整实现

```typescript
// src/directives/safeHtml.ts
import type { Directive, DirectiveBinding } from "vue";

// ============ 白名单配置 ============

/** 允许的标签及其创建函数 */
const defaultAllowedTags: Record<string, () => HTMLElement> = {
  strong: () => document.createElement("strong"),
  em: () => document.createElement("em"),
  br: () => document.createElement("br"),
  p: () => document.createElement("p"),
  ul: () => document.createElement("ul"),
  ol: () => document.createElement("ol"),
  li: () => document.createElement("li"),
  span: () => document.createElement("span"),
  a: () => document.createElement("a"),
  img: () => document.createElement("img"),
  code: () => document.createElement("code"),
  pre: () => document.createElement("pre"),
  blockquote: () => document.createElement("blockquote"),
  h1: () => document.createElement("h1"),
  h2: () => document.createElement("h2"),
  h3: () => document.createElement("h3"),
  h4: () => document.createElement("h4"),
  h5: () => document.createElement("h5"),
  h6: () => document.createElement("h6"),
  table: () => document.createElement("table"),
  tr: () => document.createElement("tr"),
  td: () => document.createElement("td"),
  th: () => document.createElement("th"),
  thead: () => document.createElement("thead"),
  tbody: () => document.createElement("tbody"),
  div: () => document.createElement("div"),
};

/** 每个标签允许的属性 */
const defaultAllowedAttributes: Record<string, string[]> = {
  a: ["href", "title", "target", "rel"],
  img: ["src", "alt", "title", "width", "height"],
  table: ["border", "cellpadding", "cellspacing"],
  td: ["colspan", "rowspan"],
  th: ["colspan", "rowspan"],
  span: [],
  code: [],
  pre: [],
  blockquote: [],
  p: [],
  h1: [],
  h2: [],
  h3: [],
  h4: [],
  h5: [],
  h6: [],
  strong: [],
  em: [],
  br: [],
  ul: [],
  ol: [],
  li: [],
  div: [],
  tr: [],
  thead: [],
  tbody: [],
};

// ============ 类型定义 ============

interface SafeHtmlConfig {
  allowedTags?: Record<string, () => HTMLElement>;
  allowedAttributes?: Record<string, string[]>;
  stripEvents?: boolean;
  stripScripts?: boolean;
}

type DirectiveBindingValue =
  | string
  | {
      html: string;
      tags?: Record<string, () => HTMLElement>;
      attrs?: Record<string, string[]>;
    };

// ============ 核心 HTML 解析器 ============

/**
 * 纯原生安全解析 HTML 字符串为 DocumentFragment
 * 使用状态机遍历 HTML 字符串，逐字符解析
 */
function parseHtmlToFragment(
  html: string,
  config: SafeHtmlConfig = {},
): DocumentFragment {
  const {
    allowedTags = defaultAllowedTags,
    allowedAttributes = defaultAllowedAttributes,
    stripEvents = true,
    stripScripts = true,
  } = config;

  const fragment = document.createDocumentFragment();
  const stack: Array<{ tagName: string; element: HTMLElement }> = [];
  let currentParent = fragment;

  const len = html.length;
  let i = 0;

  // HTML 实体解码器（使用浏览器原生能力）
  const decodeEntity = (str: string): string => {
    const temp = document.createElement("div");
    temp.innerHTML = str;
    return temp.textContent || str;
  };

  while (i < len) {
    // ---- 处理文本节点 ----
    if (html[i] !== "<") {
      const start = i;
      while (i < len && html[i] !== "<") i++;
      const text = html.slice(start, i);
      if (text) {
        const textNode = document.createTextNode(decodeEntity(text));
        currentParent.appendChild(textNode);
      }
      continue;
    }

    // ---- 处理注释 <!-- --> ----
    if (i + 1 < len && html[i + 1] === "!") {
      const end = html.indexOf("-->", i);
      if (end === -1) break;
      i = end + 3;
      continue;
    }

    // ---- 处理闭合标签 </xxx> ----
    if (i + 1 < len && html[i + 1] === "/") {
      const end = html.indexOf(">", i);
      if (end === -1) break;

      const tagName = html
        .slice(i + 2, end)
        .trim()
        .toLowerCase();

      const popped = stack.pop();
      if (popped?.tagName === tagName) {
        currentParent =
          stack.length > 0 ? stack[stack.length - 1].element : fragment;
      }
      i = end + 1;
      continue;
    }

    // ---- 处理开始标签 <xxx ...> ----
    const end = html.indexOf(">", i);
    if (end === -1) break;

    const tagPart = html.slice(i + 1, end);
    const firstSpace = tagPart.indexOf(" ");
    const rawTagName =
      firstSpace === -1 ? tagPart.trim() : tagPart.slice(0, firstSpace).trim();
    const lowerTagName = rawTagName.toLowerCase();

    // 不在白名单中的标签直接跳过（内容保留，标签移除）
    if (!allowedTags[lowerTagName]) {
      i = end + 1;
      continue;
    }

    // script 标签强制过滤
    if (stripScripts && lowerTagName === "script") {
      i = end + 1;
      continue;
    }

    const createElementFn = allowedTags[lowerTagName];
    const element =
      typeof createElementFn === "function"
        ? createElementFn()
        : document.createElement(lowerTagName);

    // 解析属性
    const attrsStr = firstSpace === -1 ? "" : tagPart.slice(firstSpace).trim();
    const attrRegex = /(\w+)=(["'])([^"']*?)\2/g;
    let match;

    while ((match = attrRegex.exec(attrsStr)) !== null) {
      const [, name, , value] = match;
      const lowerName = name.toLowerCase();

      // 过滤事件属性 onXXX
      if (stripEvents && lowerName.startsWith("on")) continue;

      // 过滤 javascript: 危险协议
      if (
        (lowerName === "href" || lowerName === "src") &&
        value.toLowerCase().startsWith("javascript:")
      ) {
        continue;
      }

      // 过滤 data: 协议（防 base64 XSS）
      if (lowerName === "src" && value.toLowerCase().startsWith("data:")) {
        continue;
      }

      // 检查属性是否在白名单中
      if (allowedAttributes[lowerTagName]?.includes(lowerName)) {
        const decodedValue = decodeEntity(value);
        element.setAttribute(lowerName, decodedValue);
      }
    }

    currentParent.appendChild(element);
    stack.push({ tagName: lowerTagName, element });
    currentParent = element;

    i = end + 1;
  }

  return fragment;
}

// ============ 渲染函数 ============

/**
 * 将安全 HTML 渲染到目标元素
 */
function renderSafeHTML(
  el: HTMLElement,
  html: string,
  config: SafeHtmlConfig,
): void {
  // 清空已有内容
  while (el.firstChild) {
    el.removeChild(el.firstChild);
  }

  if (!html || typeof html !== "string") return;

  try {
    const fragment = parseHtmlToFragment(html, config);
    el.appendChild(fragment);
  } catch (error) {
    console.warn("v-safe-html: HTML 解析失败，内容已清空", error);
    el.textContent = ""; // fail-safe：出错时清空
  }
}

// ============ 解析指令绑定值 ============

function parseBindingValue(value: DirectiveBindingValue): {
  htmlString: string;
  userConfig: Partial<SafeHtmlConfig>;
} {
  let htmlString = "";
  let userConfig: Partial<SafeHtmlConfig> = {};

  if (typeof value === "string") {
    htmlString = value;
  } else if (typeof value === "object" && value !== null) {
    htmlString = value.html ?? "";
    userConfig = {
      allowedTags: value.tags,
      allowedAttributes: value.attrs,
    };
  }

  return { htmlString, userConfig };
}

// ============ 指令导出 ============

export const vSafeHtml: Directive<HTMLElement, DirectiveBindingValue> = {
  mounted(el: HTMLElement, binding: DirectiveBinding) {
    const { htmlString, userConfig } = parseBindingValue(binding.value);

    const config: SafeHtmlConfig = {
      allowedTags: { ...defaultAllowedTags, ...userConfig.allowedTags },
      allowedAttributes: {
        ...defaultAllowedAttributes,
        ...userConfig.allowedAttributes,
      },
      stripEvents: true,
      stripScripts: true,
    };

    renderSafeHTML(el, htmlString, config);
  },

  updated(el: HTMLElement, binding: DirectiveBinding) {
    const { htmlString, userConfig } = parseBindingValue(binding.value);

    const config: SafeHtmlConfig = {
      allowedTags: { ...defaultAllowedTags, ...userConfig.allowedTags },
      allowedAttributes: {
        ...defaultAllowedAttributes,
        ...userConfig.allowedAttributes,
      },
      stripEvents: true,
      stripScripts: true,
    };

    renderSafeHTML(el, htmlString, config);
  },
};

export default vSafeHtml;
```

### 2.4 使用示例

```vue
<template>
  <div>
    <!-- 基本用法：替代 v-html -->
    <div v-safe-html="richText"></div>

    <!-- 自定义白名单 -->
    <div
      v-safe-html="{
        html: userContent,
        tags: { div: () => document.createElement('div') },
        attrs: { div: ['class'] },
      }"
    ></div>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { vSafeHtml } from "@/directives/safeHtml";

const richText = ref("<p>Hello <strong>World</strong></p>");
const userContent = ref('<div class="content">安全内容</div>');
</script>
```

### 2.5 安全测试用例

```typescript
// 以下输入都会被安全过滤：
const testCases = [
  // script 注入 → 标签被移除
  '<script>alert("xss")</script>',

  // 事件属性注入 → onerror 被移除
  '<img src=x onerror="alert(1)">',

  // javascript 协议 → href 被移除
  '<a href="javascript:alert(1)">click</a>',

  // data URI 注入 → src 被移除
  '<img src="data:text/html,<script>alert(1)</script>">',

  // 未知标签 → 标签被移除，文本内容保留
  '<iframe src="evil.com">text</iframe>',

  // 嵌套标签 → 正常解析
  "<p>段落 <strong>加粗 <em>斜体</em></strong></p>",
];
```

---

## 3. 全局注册方式

### 3.1 方式一：`<script setup>` 自动注册（Vue 3.2+ 推荐）

在 `<script setup>` 中以 `v` 开头的驼峰变量会自动作为指令可用：

```vue
<script setup lang="ts">
import { vDebounce } from "@/directives/debounce";
import { vSafeHtml } from "@/directives/safeHtml";
// 无需额外注册，模板中直接使用 v-debounce 和 v-safe-html
</script>
```

### 3.2 方式二：全局注册（main.ts）

```typescript
// main.ts
import { createApp } from "vue";
import App from "./App.vue";
import { vDebounce } from "@/directives/debounce";
import { vSafeHtml } from "@/directives/safeHtml";

const app = createApp(App);

// 全局注册指令
app.directive("debounce", vDebounce);
app.directive("safe-html", vSafeHtml);

app.mount("#app");
```

### 3.3 方式三：插件式注册

```typescript
// src/directives/index.ts
import type { App } from "vue";
import { vDebounce } from "./debounce";
import { vSafeHtml } from "./safeHtml";

export function registerDirectives(app: App): void {
  app.directive("debounce", vDebounce);
  app.directive("safe-html", vSafeHtml);
}

// main.ts
import { registerDirectives } from "@/directives";
registerDirectives(app);
```

---

## 4. 异构项目适配指南

### 4.1 React 项目

React 没有指令系统，但可以用自定义 Hook 或高阶组件实现等效功能：

```typescript
// useDebounce Hook（React 等效实现）
import { useRef, useCallback } from 'react';

function useDebounce<T extends (...args: any[]) => void>(
  handler: T,
  delay: number = 500,
  immediate: boolean = false
): T {
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  return useCallback(
    (...args: Parameters<T>) => {
      if (immediate) {
        if (timeoutRef.current) {
          clearTimeout(timeoutRef.current);
        } else {
          handler(...args);
        }
      } else if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }

      timeoutRef.current = setTimeout(() => {
        if (!immediate) {
          handler(...args);
        }
        timeoutRef.current = null;
      }, delay);
    },
    [handler, delay, immediate]
  ) as T;
}

// 使用
function MyComponent() {
  const handleClick = useDebounce(() => {
    console.log('防抖点击');
  }, 500);

  return <button onClick={handleClick}>提交</button>;
}
```

### 4.2 Angular 项目

Angular 使用指令装饰器实现等效功能：

```typescript
// debounce.directive.ts
import { Directive, Input, HostListener, OnDestroy } from "@angular/core";

@Directive({
  selector: "[appDebounce]",
})
export class DebounceDirective implements OnDestroy {
  @Input("appDebounce") handler!: () => void;
  @Input("appDebounceDelay") delay: number = 500;
  @Input("appDebounceImmediate") immediate: boolean = false;

  private timeout: ReturnType<typeof setTimeout> | null = null;

  @HostListener("click")
  onClick() {
    if (this.immediate) {
      if (this.timeout) {
        clearTimeout(this.timeout);
      } else {
        this.handler();
      }
    } else if (this.timeout) {
      clearTimeout(this.timeout);
    }

    this.timeout = setTimeout(() => {
      if (!this.immediate) {
        this.handler();
      }
      this.timeout = null;
    }, this.delay);
  }

  ngOnDestroy() {
    if (this.timeout) {
      clearTimeout(this.timeout);
    }
  }
}
```

### 4.3 原生 JavaScript 项目

直接封装为工具函数：

```typescript
// utils/debounce.ts
export function debounce<T extends (...args: any[]) => void>(
  handler: T,
  delay: number = 500,
  immediate: boolean = false,
): (...args: Parameters<T>) => void {
  let timeout: ReturnType<typeof setTimeout> | null = null;

  return function (this: any, ...args: Parameters<T>) {
    if (immediate) {
      if (timeout) {
        clearTimeout(timeout);
      } else {
        handler.apply(this, args);
      }
    } else if (timeout) {
      clearTimeout(timeout);
    }

    timeout = setTimeout(() => {
      if (!immediate) {
        handler.apply(this, args);
      }
      timeout = null;
    }, delay);
  };
}
```

---

## 5. 快速接入检查清单

- [ ] 创建 `src/directives/debounce.ts`，复制防抖指令代码
- [ ] 创建 `src/directives/safeHtml.ts`，复制安全 HTML 指令代码
- [ ] 选择注册方式：`<script setup>` 自动注册 / 全局注册 / 插件式注册
- [ ] 替换项目中所有 `v-html` 为 `v-safe-html`
- [ ] 为所有提交按钮、搜索输入框添加 `v-debounce`
- [ ] 在 ESLint 配置中启用 `vue/no-v-html: 2` 规则禁止 v-html
- [ ] 编写安全测试用例验证 v-safe-html 过滤效果
