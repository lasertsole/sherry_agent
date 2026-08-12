# errorCaptured 工厂函数 — 可复用实现指南

> 本文档从 RetailMiniAPP 项目中提炼，提供了 Vue 3 组件级错误捕获的工厂函数实现，实现统一 toast 提示 + 日志记录 + 错误传播控制。

---

## 目录

1. [设计思路](#1-设计思路)
2. [完整实现](#2-完整实现)
3. [使用方式](#3-使用方式)
4. [Toast 组件接口约定](#4-toast-组件接口约定)
5. [日志工具对接](#5-日志工具对接)
6. [异构项目适配指南](#6-异构项目适配指南)

---

## 1. 设计思路

### 1.1 解决什么问题

Vue 组件运行时可能抛出各种错误（API 调用异常、数据解析失败、第三方组件错误等）。如果没有统一捕获：

- 错误会冒泡到根组件，导致整个应用白屏
- 用户看不到任何错误提示，体验差
- 错误信息无法被记录到日志系统

### 1.2 方案核心

利用 Vue 3 的 `onErrorCaptured` 生命周期钩子，封装为工厂函数：

```
子组件抛出错误
  │
  ▼
onErrorCaptured 钩子触发
  │
  ├─ 1. 错误信息标准化（Error / string / object → 字符串）
  │
  ├─ 2. 日志记录（logUtils.e 输出错误 + 组件名）
  │
  ├─ 3. Toast 提示（调用 msgRef.open 显示给用户）
  │
  └─ 4. return false 阻止错误继续向上传播
```

### 1.3 为什么用工厂函数

- **复用性**：每个需要错误捕获的组件只需调用一次工厂函数，传入自己的 Toast 组件 ref
- **一致性**：所有组件的错误处理逻辑完全一致
- **可扩展性**：可以轻松添加新的错误处理逻辑（如上报到监控系统）

---

## 2. 完整实现

### 2.1 依赖项

```bash
npm install lodash
npm install -D @types/lodash
```

### 2.2 核心代码

````typescript
// src/hooks/errorCaptured.ts
import type { ComponentPublicInstance, Ref } from "vue";
import { onErrorCaptured } from "vue";
import { isEmpty } from "lodash";

// ============ 类型定义 ============

/**
 * Toast 组件的 ref 类型约定
 * 任何 Toast/Message 组件只要实现 open 方法即可接入
 */
export interface MsgRefType {
  /**
   * 显示提示消息
   * @param obj.message 消息内容
   * @param obj.type    消息类型（text / success / error / warning）
   * @param obj.position 位置（top / bottom / center）
   * @param obj.duration 显示时长（ms）
   */
  open: (obj: {
    message: string;
    type?: string;
    position?: string;
    duration?: number;
  }) => void;

  /** 可选的扩展方法 */
  openMessage?: (
    type: string,
    message: string,
    otherParams?: OtherParams,
  ) => void;
}

interface OtherParams {
  [key: string]: unknown;
}

// ============ 工厂函数 ============

/**
 * 创建 onErrorCaptured 钩子
 *
 * @param msgRef Toast/Message 组件的 ref，用于显示错误提示
 * @returns onErrorCaptured 的返回值（可直接在 setup 中使用）
 *
 * @example
 * ```vue
 * <script setup lang="ts">
 * import { ref } from 'vue';
 * import { onErrorCapturedFactory, type MsgRefType } from '@/hooks/errorCaptured';
 *
 * const msgRef = ref<MsgRefType>();
 * onErrorCapturedFactory(msgRef);
 * </script>
 *
 * <template>
 *   <Toast ref="msgRef" />
 *   <ChildComponent />
 * </template>
 * ```
 */
export function onErrorCapturedFactory(msgRef: Ref<MsgRefType | undefined>) {
  return onErrorCaptured(
    (err: unknown, instance: ComponentPublicInstance | null) => {
      // ---- 1. 错误信息标准化 ----
      let errMsg: string = "";
      let target: string = "";

      if (err instanceof Error) {
        errMsg = err.message;
      } else if (typeof err === "string") {
        errMsg = err;
      } else {
        // 处理对象类型错误（可能包含循环引用）
        try {
          errMsg = JSON.stringify(err);
        } catch (e) {
          // JSON.stringify 遇到循环引用会抛出 TypeError
          errMsg = String(err);
          logUtil.e(errMsg);
          logUtil.e(`Failed to serialize err...${String(e)}`);
        }
      }

      // ---- 2. 获取出错组件名 ----
      if (!isEmpty(instance?.$options?.name)) {
        target = instance!.$options!.name!;
      } else {
        target = "unknown component";
      }

      // ---- 3. 记录错误日志 ----
      logUtil.e(`${target} happened error...`, errMsg);

      // ---- 4. 显示 Toast 提示 ----
      msgRef.value?.open?.({
        message: errMsg,
        type: "text",
        position: "bottom",
        duration: 1500,
      });

      // ---- 5. 阻止错误继续向上传播 ----
      return false;
    },
  );
}
````

### 2.3 日志工具（简化版，完整版见 `01-代码规范与质量.md`）

工厂函数依赖 `logUtil` 进行错误日志输出。以下是最小化实现：

```typescript
// src/utils/log.ts

declare const ENV_MODE: string;

export class EnvironmentUtils {
  public static isLocal(): boolean {
    return ENV_MODE === "local";
  }
  public static isProd(): boolean {
    return ENV_MODE === "prod";
  }
}

export class SimpleLogger {
  public l(...args: any[]): void {
    if (!EnvironmentUtils.isProd()) console.log(...args);
  }
  public w(...args: any[]): void {
    if (!EnvironmentUtils.isProd()) console.warn(...args);
  }
  public e(...args: any[]): void {
    if (!EnvironmentUtils.isLocal()) {
      console.error(...args);
    } else {
      console.warn("local环境报错", ...args);
    }
  }
}

export const logUtil = new SimpleLogger();
```

---

## 3. 使用方式

### 3.1 基本用法

```vue
<!-- ParentComponent.vue -->
<template>
  <div>
    <!-- Toast 组件，用于显示错误提示 -->
    <Toast ref="msgRef" />

    <!-- 子组件的错误会被自动捕获 -->
    <ChildComponent />
    <AnotherComponent />
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { onErrorCapturedFactory, type MsgRefType } from "@/hooks/errorCaptured";
import Toast from "@/components/Toast.vue";

// 创建 Toast ref
const msgRef = ref<MsgRefType>();

// 一行代码注册错误捕获
onErrorCapturedFactory(msgRef);
</script>
```

### 3.2 子组件触发错误

```vue
<!-- ChildComponent.vue -->
<template>
  <div>
    <button @click="triggerError">触发错误</button>
    <button @click="triggerApiError">模拟 API 错误</button>
  </div>
</template>

<script setup lang="ts">
const triggerError = () => {
  // 直接抛出 Error
  throw new Error("数据加载失败");
};

const triggerApiError = () => {
  // 模拟 API 调用失败
  fetch("/api/data")
    .then((res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    })
    .then((data) => {
      console.log(data);
    })
    .catch((err) => {
      // 错误会被父组件的 onErrorCaptured 捕获
      throw err;
    });
};
</script>
```

### 3.3 多层级组件中的传播控制

```
App.vue (onErrorCapturedFactory — 捕获所有子组件错误)
  └─ Layout.vue (没有 errorCaptured — 错误继续冒泡)
       └─ PageA.vue (onErrorCapturedFactory — 捕获 PageA 子组件错误)
            └─ WidgetA.vue (抛出错误)
                 │
                 ▼
            PageA 的 onErrorCaptured 触发
            return false → 错误不再向上冒泡到 Layout 和 App
```

**关键行为**：`return false` 会阻止错误继续向上传播。如果希望错误同时被多级父组件捕获，将 `return false` 改为 `return true` 或不返回值。

---

## 4. Toast 组件接口约定

工厂函数通过 `msgRef.value?.open?.()` 调用 Toast 组件。任何 Toast 组件只需暴露 `open` 方法即可接入。

### 4.1 自定义 Toast 组件示例

```vue
<!-- src/components/Toast.vue -->
<template>
  <Transition name="toast">
    <div
      v-if="visible"
      :class="['toast', `toast--${type}`, `toast--${position}`]"
    >
      {{ message }}
    </div>
  </Transition>
</template>

<script setup lang="ts">
import { ref } from "vue";

const visible = ref(false);
const message = ref("");
const type = ref("text");
const position = ref("bottom");
let timer: ReturnType<typeof setTimeout> | null = null;

const open = (options: {
  message: string;
  type?: string;
  position?: string;
  duration?: number;
}) => {
  message.value = options.message;
  type.value = options.type || "text";
  position.value = options.position || "bottom";

  visible.value = true;

  if (timer) clearTimeout(timer);
  timer = setTimeout(() => {
    visible.value = false;
  }, options.duration || 1500);
};

// 暴露 open 方法给父组件
defineExpose({ open });
</script>

<style scoped>
.toast {
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  padding: 8px 16px;
  border-radius: 4px;
  font-size: 14px;
  color: #fff;
  background: rgba(0, 0, 0, 0.7);
  z-index: 9999;
}
.toast--bottom {
  bottom: 20%;
}
.toast--top {
  top: 20%;
}
.toast--center {
  top: 50%;
  transform: translate(-50%, -50%);
}
.toast--error {
  background: rgba(245, 63, 63, 0.9);
}
.toast--success {
  background: rgba(34, 197, 94, 0.9);
}

.toast-enter-active,
.toast-leave-active {
  transition: opacity 0.3s;
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
}
</style>
```

### 4.2 使用第三方 Toast 库

如果项目使用 Element Plus、Ant Design Vue、Vant 等组件库，可以适配为符合 `MsgRefType` 接口：

```typescript
// 方式一：使用 Element Plus 的 ElMessage
import { ElMessage } from "element-plus";

const msgRef = ref<MsgRefType>({
  open: (options) => {
    ElMessage({
      message: options.message,
      type: options.type === "error" ? "error" : "info",
      duration: options.duration || 1500,
    });
  },
});

// 方式二：使用 Vant 的 showToast
import { showToast } from "vant";

const msgRef = ref<MsgRefType>({
  open: (options) => {
    showToast({
      message: options.message,
      position: options.position || "bottom",
      duration: options.duration || 1500,
    });
  },
});

// 方式三：使用 Ant Design Vue 的 message
import { message } from "ant-design-vue";

const msgRef = ref<MsgRefType>({
  open: (options) => {
    message.error(options.message, (options.duration || 1.5) / 1000);
  },
});
```

---

## 5. 日志工具对接

工厂函数使用 `logUtil.e()` 记录错误日志。根据项目需求，可以对接不同的日志系统：

### 5.1 对接 Sentry / Bugsnab 等监控平台

```typescript
// src/hooks/errorCaptured.ts (增强版)
import * as Sentry from "@sentry/vue";

export function onErrorCapturedFactory(msgRef: Ref<MsgRefType | undefined>) {
  return onErrorCaptured(
    (err: unknown, instance: ComponentPublicInstance | null) => {
      let errMsg = "";
      let target = "";

      // ... 错误标准化逻辑同上 ...

      // ---- 记录到 Sentry ----
      Sentry.captureException(err, {
        tags: { component: target },
        extra: { errMsg },
      });

      // ---- 记录到本地日志 ----
      logUtil.e(`${target} happened error...`, errMsg);

      // ---- Toast 提示 ----
      msgRef.value?.open?.({
        message: errMsg,
        type: "text",
        position: "bottom",
        duration: 1500,
      });

      return false;
    },
  );
}
```

### 5.2 对接自建日志上报服务

```typescript
// 在工厂函数中增加上报逻辑
function reportError(component: string, error: string): void {
  // 上报到日志服务
  fetch("/api/log/error", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      component,
      error,
      timestamp: new Date().toISOString(),
      url: window.location.href,
      userAgent: navigator.userAgent,
    }),
  }).catch(() => {
    // 上报失败不影响用户体验
  });
}

export function onErrorCapturedFactory(msgRef: Ref<MsgRefType | undefined>) {
  return onErrorCaptured((err: unknown, instance) => {
    // ... 标准化逻辑 ...

    reportError(target, errMsg); // 上报到服务端
    logUtil.e(`${target} happened error...`, errMsg); // 本地日志
    msgRef.value?.open?.({
      message: errMsg,
      type: "text",
      position: "bottom",
      duration: 1500,
    });

    return false;
  });
}
```

---

## 6. 异构项目适配指南

### 6.1 React 项目

React 使用 Error Boundary 组件实现等效功能：

```typescript
// ErrorBoundary.tsx
import { Component, ReactNode, ErrorInfo } from 'react';

interface Props {
  children: ReactNode;
  onError?: (error: Error, info: ErrorInfo) => void;
  fallback?: (error: Error, reset: () => void) => ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 日志记录
    console.error('ErrorBoundary caught:', error, info);

    // 上报
    this.props.onError?.(error, info);
  }

  reset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    if (this.state.error) {
      if (this.props.fallback) {
        return this.props.fallback(this.state.error, this.reset);
      }
      return (
        <div style={{ padding: 16, color: 'red' }}>
          {this.state.error.message}
          <button onClick={this.reset}>重试</button>
        </div>
      );
    }
    return this.props.children;
  }
}

// 使用
function App() {
  return (
    <ErrorBoundary
      onError={(error, info) => {
        // 上报到 Sentry
        Sentry.captureException(error, { extra: info });
      }}
      fallback={(error, reset) => (
        <div>
          <p>{error.message}</p>
          <button onClick={reset}>重试</button>
        </div>
      )}
    >
      <ChildComponent />
    </ErrorBoundary>
  );
}
```

### 6.2 Angular 项目

Angular 使用 ErrorHandler 全局错误处理：

```typescript
// error-handler.ts
import { ErrorHandler, Injectable } from "@angular/core";

@Injectable()
export class GlobalErrorHandler implements ErrorHandler {
  handleError(error: unknown): void {
    let errMsg = "";
    if (error instanceof Error) {
      errMsg = error.message;
    } else {
      errMsg = String(error);
    }

    // 记录日志
    console.error("Global error:", errMsg);

    // 显示 Toast（通过注入 ToastService）
    // this.toastService.show(errMsg);
  }
}

// app.module.ts
import { ErrorHandler } from "@angular/core";

@NgModule({
  providers: [{ provide: ErrorHandler, useClass: GlobalErrorHandler }],
})
export class AppModule {}
```

### 6.3 原生 JavaScript 项目

使用 `window.onerror` 和 `unhandledrejection` 全局捕获：

```typescript
// error-handler.ts
class ErrorHandler {
  private onErrorCallback?: (message: string, source: string) => void;

  init(onError?: (message: string, source: string) => void): void {
    this.onErrorCallback = onError;

    // 捕获同步错误
    window.onerror = (message, source, lineno, colno, error) => {
      const errMsg = error?.message || String(message);
      console.error("Window error:", errMsg, `at ${source}:${lineno}:${colno}`);
      this.onErrorCallback?.(errMsg, source || "");
      return true; // 阻止默认错误处理
    };

    // 捕获未处理的 Promise rejection
    window.addEventListener("unhandledrejection", (event) => {
      const errMsg = event.reason?.message || String(event.reason);
      console.error("Unhandled rejection:", errMsg);
      this.onErrorCallback?.(errMsg, "");
    });
  }
}

export const errorHandler = new ErrorHandler();

// 使用
errorHandler.init((message) => {
  // 显示 Toast
  alert(message);
});
```

---

## 7. 快速接入检查清单

- [ ] 创建 `src/hooks/errorCaptured.ts`，复制工厂函数代码
- [ ] 创建 `src/utils/log.ts`，配置环境感知日志工具
- [ ] 确保项目中有 Toast/Message 组件（自定义或第三方）
- [ ] 在顶层父组件中调用 `onErrorCapturedFactory(msgRef)`
- [ ] 确认 `msgRef` 的 `open` 方法签名与 `MsgRefType` 接口一致
- [ ] 测试：在子组件中 `throw new Error('test')` 验证捕获链路
- [ ] 可选：对接 Sentry/Bugsnag 等监控平台
- [ ] 可选：增加错误上报服务端接口
