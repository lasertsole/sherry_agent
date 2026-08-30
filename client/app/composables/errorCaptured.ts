import type { ComponentPublicInstance, Ref } from 'vue';
import { onErrorCaptured } from 'vue';
import { isEmpty } from 'lodash-es';
import { useI18n } from 'vue-i18n';
import { toastError } from '~/composables/toast';
import { logUtil } from '~/utils/log';

/**
 * errorCaptured factory function (extracted from doc 03, the "errorCaptured factory
 * function" document; Nuxt 4 adapted version).
 *
 * Capture chain:
 *   A child component throws an error
 *     → the onErrorCaptured hook fires
 *     → 1. Normalize the error info (Error / string / object → string, with circular
 *        reference handling)
 *     → 2. Log the failing component name + write a log (logUtil.e; console.error is
 *        persisted by clientLog.ts)
 *     → 3. Toast notification (global toastError by default; a custom Toast ref
 *        conforming to MsgRefType can be injected)
 *     → 4. return false stops the error from propagating further up (avoids bubbling
 *        to the root component and blanking the whole page)
 *
 * Note: onErrorCaptured only captures errors from **descendant components** (Vue
 * walks up from instance.parent); errors in this component itself are captured by an
 * identical hook further up — each page calls this factory, forming layered capture
 * (see doc 03 §3.3 multi-level propagation control).
 */

/**
 * Ref type contract for Toast components (doc 03 §4).
 * Any Toast/Message component is plug-compatible as long as it implements `open`.
 */
export interface MsgRefType {
  /**
   * Show a notification message
   * @param obj.message   Message content
   * @param obj.type      Message type (text / success / error / warning)
   * @param obj.position  Position (top / bottom / center)
   * @param obj.duration  Display duration (ms)
   */
  open: (obj: { message: string; type?: string; position?: string; duration?: number }) => void;
}

/** i18n key for errors.pageError (configured in all four locale files). */
const PAGE_ERROR_KEY = 'errors.pageError';

/**
 * Create a page-level onErrorCaptured hook. Call once at the top of a page/component's
 * `<script setup>`.
 *
 * @param msgRef Optional custom Toast ref (must implement MsgRefType.open);
 *               when omitted, the project's global toast layer is used
 *               (toastError from ~/composables/toast).
 * @returns The return value of onErrorCaptured (usable directly in setup; usually
 *   no need to keep it)
 *
 * @example
 * ```vue
 * <script setup lang="ts">
 * // Page-level error capture: child component errors → log + toast, blocking upward bubbling
 * useErrorCaptured();
 * </script>
 * ```
 */
export function useErrorCaptured(msgRef?: Ref<MsgRefType | undefined>) {
  // Capture the i18n composer's t during setup: outside a setup context, the proxy
  // nuxt-i18n v10 attaches at nuxtApp.$i18n exposes no t (and no .global), so
  // toast.ts's safeT `$i18n.global.t` cannot get a translation (it returns the raw
  // key). This factory is only called within setup, so capture it here upfront
  // (same useI18n usage as useSubagentTasks.ts).
  const { t } = useI18n();

  return onErrorCaptured((err: unknown, instance: ComponentPublicInstance | null) => {
    // ---- 1. Normalize the error info ----
    let errMsg: string;

    if (err instanceof Error) {
      errMsg = err.message;
    } else if (typeof err === 'string') {
      errMsg = err;
    } else {
      // Handle object-type errors (may contain circular references)
      try {
        errMsg = JSON.stringify(err);
      } catch (e) {
        // JSON.stringify throws a TypeError on circular references
        errMsg = String(err);
        logUtil.e(`Failed to serialize err...${String(e)}`);
      }
    }

    // ---- 2. Get the failing component's name ----
    // The SFC compilation output of `<script setup>` puts the component name on
    // `__name` (`$options.name` is only populated for defineOptions/Options API
    // components); fall back in order: name → __name → 'unknown component'
    const opts: Partial<{ name: string; __name: string }> | undefined = instance?.$options;
    const rawName = opts?.name || opts?.__name;
    const target = !isEmpty(rawName) ? String(rawName) : 'unknown component';

    // ---- 3. Write the error log ----
    logUtil.e(`${target} happened error...`, errMsg);

    // ---- 4. Show the toast notification ----
    if (msgRef) {
      msgRef.value?.open?.({
        message: errMsg,
        type: 'text',
        position: 'bottom',
        duration: 1500
      });
    } else {
      toastError(t(PAGE_ERROR_KEY), errMsg);
    }

    // ---- 5. Stop the error from propagating further up ----
    return false;
  });
}
