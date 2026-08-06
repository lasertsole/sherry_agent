<template>
     <div
        class="root"
        tabindex="-1"
    >
        <div
            class="inputBox"
            contenteditable="true"
            :contenteditable="!sending"
            :aria-disabled="sending"
            :placeholder="sending ? 'AI 正在思考中...' : defaultHint"
            ref="inputDom"
            @input.stop="inputFunc($event)"
            @keydown.enter.stop.prevent="handleKeyEnter($event)"
        >
        </div>

        <Button
            v-if="!sending"
            label="发送"
            class="send"
            :disabled="!sendingAllowed"
            @click="handleSend"
        />
        <Button
            v-else
            label="停止"
            class="send"
            severity="danger"
            @click="emit('stop')"
        />
    </div>
</template>

<script lang="ts" setup>
import { computed, type ShallowRef } from 'vue';
import { isEmpty } from 'lodash-es';

const defaultHint = '请输入内容...';

/** 是否处于 AI 回复生成中（由父组件控制，防止重复发送） */
const props = withDefaults(defineProps<Props>(), {
  sending: false,
});

interface Props {
  sending?: boolean;
}

const emit = defineEmits<{
  /** 发送消息，载荷为纯文本内容 */
  (e: 'send', text: string): void;
  /** 停止当前 AI 回复生成 */
  (e: 'stop'): void;
}>();

/** 输入区 DOM */
const inputDom: ShallowRef<HTMLElement | null> = useTemplateRef('inputDom');

/** 当前输入内容（纯文本，便于校验是否为空） */
const textContent = ref('');

/** 是否允许发送（非空且非生成中） */
const sendingAllowed = computed(() => !isEmpty(textContent.value));

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
    textContent.value = '';
  } else {
    textContent.value = text.trim();
  }
}

/** Enter 发送（无 Shift），Shift+Enter 保留换行 */
function handleKeyEnter(event: KeyboardEvent): void {
  if (event.shiftKey) return; // Shift+Enter 换行
  handleSend();
}

/** 发送：校验→清空输入区→向上抛出文本 */
function handleSend(): void {
  if (!sendingAllowed.value || props.sending) return;
  const text = textContent.value;
  if (isEmpty(text)) return;

  clearInput();
  emit('send', text);
}

/** 清空输入区 */
function clearInput(): void {
  if (inputDom.value) inputDom.value.innerHTML = '';
  textContent.value = '';
}
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

