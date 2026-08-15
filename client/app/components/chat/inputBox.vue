<template>
     <div
        class="root"
        tabindex="-1"
    >
        <div
            class="inputBox"
            contenteditable="true"
            :contenteditable="!sending && !disabled"
            :aria-disabled="sending || disabled"
            :placeholder="placeholderText"
            ref="inputDom"
            @input.stop="inputFunc($event)"
            @keydown.enter.stop.prevent="handleKeyEnter($event)"
        >
        </div>

        <Button
            v-if="!sending"
            :label="t('chatInput.send')"
            class="send"
            :disabled="!sendingAllowed || disabled"
            @click="handleSend"
        />
        <Button
            v-else
            :label="t('chatInput.stop')"
            class="send"
            severity="danger"
            @click="emit('stop')"
        />
    </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, watch, type ShallowRef } from 'vue';
import { isEmpty } from 'lodash-es';
import { useI18n } from 'vue-i18n';

const { t } = useI18n();

/** 是否处于 AI 回复生成中（由父组件控制，防止重复发送） */
const props = withDefaults(defineProps<Props>(), {
  sending: false,
  disabled: false,
  disabledText: '',
});

interface Props {
  sending?: boolean;
  /** 是否禁止输入（如存在待审批的 HITL 请求时，阻断手动输入/发送） */
  disabled?: boolean;
  /** 禁用状态下显示的 placeholder 文案（如"等待审批…"） */
  disabledText?: string;
}

const emit = defineEmits<{
  /** 发送消息，载荷为纯文本内容 */
  (e: 'send', text: string): void;
  /** 停止当前 AI 回复生成 */
  (e: 'stop'): void;
}>();

/** 输入区 DOM */
const inputDom: ShallowRef<HTMLElement | null> = useTemplateRef('inputDom');

/**
 * 受控草稿内容（由父组件通过 `v-model:draft` 双向绑定）。
 *
 * 父组件（per-session ChatPage）在切换会话时会把该会话的草稿写回，
 * 从而在 KeepAlive 缓存下保留每个会话未发送的输入内容。
 */
const draft = defineModel<string>('draft', { default: '' });

/** 是否允许发送（非空且非生成中） */
const sendingAllowed = computed(() => !isEmpty(draft.value));

/** 输入区占位文案：生成中→"思考中"，禁用→传入的审批提示，否则默认提示 */
const placeholderText = computed(() => {
  if (props.sending) return t('chatInput.thinking');
  if (props.disabled) return props.disabledText || t('chatInput.placeholder');
  return t('chatInput.placeholder');
});

/** 输入回调：剥离 contenteditable 的标签，仅保留纯文本用于校验 */
function inputFunc(event: Event): void {
  if (!(event instanceof InputEvent)) {
    return;
  }

  const target = event.target as HTMLElement;
  const text = target.textContent ?? '';

  // 仅剩空行/换行时，清空内部内容避免残留 <br>
  if (isEmpty(text) || text.trim() === '') {
    target.innerHTML = '';
    draft.value = '';
  } else {
    draft.value = text.trim();
  }
}

/** Enter 发送（无 Shift），Shift+Enter 保留换行 */
function handleKeyEnter(event: KeyboardEvent): void {
  if (event.shiftKey) return; // Shift+Enter 换行
  handleSend();
}

/** 发送：校验→清空输入区→向上抛出文本 */
function handleSend(): void {
  if (!sendingAllowed.value || props.sending || props.disabled) return;
  const text = draft.value;
  if (isEmpty(text)) return;

  clearInput();
  emit('send', text);
}

/** 清空输入区 */
function clearInput(): void {
  if (inputDom.value) inputDom.value.innerHTML = '';
  draft.value = '';
}

/**
 * 将外部草稿同步到 contenteditable 输入区。
 *
 * 父组件在切换会话（KeepAlive 恢复）时调用，把该会话缓存的草稿文本
 * 渲染回输入框。仅在输入区为空时写入，避免覆盖用户正在输入的内容。
 */
function syncDraftToDom(): void {
  if (!inputDom.value) return;
  if (draft.value && inputDom.value.textContent !== draft.value) {
    inputDom.value.textContent = draft.value;
  }
}

// 父组件通过 v-model:draft 写入草稿时，同步到输入区 DOM
watch(draft, () => syncDraftToDom());

// 组件挂载后同步一次（KeepAlive 恢复时也会触发 onMounted）
onMounted(() => syncDraftToDom());
</script>

<style lang="scss" scoped>
    @use "sass:math";
    @use "@/common.scss" as common;

    .root{
        height: 100%;
        width: 100%;
        position: relative;

        >.inputBox{
            height: 100%;
            width: 100%;
            outline: none;
            word-break: break-all;
            padding: 0.5rem;
            overflow-y: auto;

            // contenteditable 占位符（伪元素实现）
            &:empty::before {
                content: attr(placeholder);
                color: #9ca3af;
                pointer-events: none;
            }
        }

        >.send{
            position: absolute;
            right: 0.5rem;
            bottom: 0.5rem;
        }
    }
</style>

