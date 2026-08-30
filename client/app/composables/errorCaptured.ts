import type { ComponentPublicInstance, Ref } from 'vue';
import { onErrorCaptured } from 'vue';
import { isEmpty } from 'lodash-es';
import { useI18n } from 'vue-i18n';
import { toastError } from '~/composables/toast';
import { logUtil } from '~/utils/log';

/**
 * errorCaptured 工厂函数（提炼自 03-errorCaptured工厂函数.md，Nuxt 4 适配版）。
 *
 * 捕获链路：
 *   子组件抛出错误
 *     → onErrorCaptured 钩子触发
 *     → 1. 错误信息标准化（Error / string / object → 字符串，兼容循环引用）
 *     → 2. 记录出错组件名 + 日志（logUtil.e；console.error 由 clientLog.ts 持久化）
 *     → 3. Toast 提示（默认全局 toastError，可注入符合 MsgRefType 的自定义 Toast ref）
 *     → 4. return false 阻止错误继续向上传播（避免冒泡到根组件导致整页白屏）
 *
 * 注意：onErrorCaptured 只捕获**后代组件**的错误（Vue 从 instance.parent 起向上
 * 遍历），本组件自身的错误由更上层的同名钩子捕获——各页面均调用本工厂形成
 * 分层捕获（见 03 文档 §3.3 多层级传播控制）。
 */

/**
 * Toast 组件的 ref 类型约定（03 文档 §4）。
 * 任何 Toast/Message 组件只要实现 `open` 方法即可接入。
 */
export interface MsgRefType {
  /**
   * 显示提示消息
   * @param obj.message   消息内容
   * @param obj.type      消息类型（text / success / error / warning）
   * @param obj.position  位置（top / bottom / center）
   * @param obj.duration  显示时长（ms）
   */
  open: (obj: {
    message: string;
    type?: string;
    position?: string;
    duration?: number;
  }) => void;
}

/** errors.pageError 的 i18n key（四个 locale 文件均已配置）。 */
const PAGE_ERROR_KEY = 'errors.pageError';

/**
 * 创建页面级 onErrorCaptured 钩子。在页面/组件的 `<script setup>` 顶部调用一次即可。
 *
 * @param msgRef 可选的自定义 Toast ref（须实现 MsgRefType.open）；
 *               缺省时使用项目全局 toast 层（~/composables/toast 的 toastError）。
 * @returns onErrorCaptured 的返回值（可直接在 setup 中使用，一般无需接收）
 *
 * @example
 * ```vue
 * <script setup lang="ts">
 * // 页面级错误捕获：子组件错误 → 日志 + toast，阻断向上冒泡
 * useErrorCaptured();
 * </script>
 * ```
 */
export function useErrorCaptured(msgRef?: Ref<MsgRefType | undefined>) {
  // setup 阶段捕获 i18n composer 的 t：非 setup 上下文里 nuxt-i18n v10 挂在
  // nuxtApp.$i18n 上的代理不暴露 t（也没有 .global），toast.ts safeT 的
  // `$i18n.global.t` 拿不到翻译（返回原始 key）。本工厂只在 setup 中调用，
  // 故在此先行捕获（与 useSubagentTasks.ts 的 useI18n 用法一致）。
  const { t } = useI18n();

  return onErrorCaptured((err: unknown, instance: ComponentPublicInstance | null) => {
    // ---- 1. 错误信息标准化 ----
    let errMsg = '';

    if (err instanceof Error) {
      errMsg = err.message;
    } else if (typeof err === 'string') {
      errMsg = err;
    } else {
      // 处理对象类型错误（可能包含循环引用）
      try {
        errMsg = JSON.stringify(err);
      } catch (e) {
        // JSON.stringify 遇到循环引用会抛出 TypeError
        errMsg = String(err);
        logUtil.e(`Failed to serialize err...${String(e)}`);
      }
    }

    // ---- 2. 获取出错组件名 ----
    // `<script setup>` 的 SFC 编译产物把组件名写在 `__name` 上（`$options.name`
    // 仅 defineOptions/选项式组件有值），依次回退：name → __name → 'unknown component'
    const opts: Partial<{ name: string; __name: string }> | undefined = instance?.$options;
    const rawName = opts?.name || opts?.__name;
    const target = !isEmpty(rawName) ? String(rawName) : 'unknown component';

    // ---- 3. 记录错误日志 ----
    logUtil.e(`${target} happened error...`, errMsg);

    // ---- 4. 显示 Toast 提示 ----
    if (msgRef) {
      msgRef.value?.open?.({
        message: errMsg,
        type: 'text',
        position: 'bottom',
        duration: 1500,
      });
    } else {
      toastError(t(PAGE_ERROR_KEY), errMsg);
    }

    // ---- 5. 阻止错误继续向上传播 ----
    return false;
  });
}
