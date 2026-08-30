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
            role="textbox"
            aria-multiline="true"
            :aria-label="t('chatInput.placeholder')"
            :placeholder="placeholderText"
            ref="inputDom"
            @input.stop="inputFunc($event)"
            @keydown.enter.stop.prevent="handleKeyEnter($event)"
            @keydown.up.stop.prevent="handleKeyArrowUp($event)"
            @keydown.down.stop.prevent="handleKeyArrowDown($event)"
            @keydown.escape.stop.prevent="handleKeyEscape($event)"
        >
        </div>

        <Button
            v-if="!sending"
            v-debounce:click.500="handleSend"
            :label="t('chatInput.send')"
            class="send"
            :disabled="!sendingAllowed || disabled"
        />
        <Button
            v-else
            v-debounce:click.300="() => emit('stop')"
            :label="t('chatInput.stop')"
            class="send"
            severity="danger"
        />
    </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, watch, ref, type ShallowRef } from 'vue';
import { isEmpty } from 'lodash-es';
import { useI18n } from 'vue-i18n';
import { vDebounce } from '~/directives/debounce';

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

/**
 * 本会话已发送用户问题的历史缓存（内存级，跟随本输入框实例/会话生命周期，
 * 由 KeepAlive 按会话隔离）。按发送顺序存储，最新一条在数组末尾，最多保留最近 10 条。
 */
const questionHistory = ref<string[]>([]);

/** 上下键浏览历史问题的最大缓存条数 */
const MAX_HISTORY = 10;

/**
 * 当前浏览位置：指向 `questionHistory` 的下标，-1 表示「未在浏览，输入框展示的是
 * 用户自己的实时草稿」。按 ↑ 从最近一条（length-1）开始逐条向前，按 ↓ 向后，
 * 越过「最早一条」后回落到 -1（恢复当前草稿）。
 */
const browseIndex = ref(-1);

/**
 * 进入浏览模式前用户正在编辑的草稿备份。按 Escape 退出浏览模式时恢复到该快照；
 * 若用户曾直接编辑输入框（inputFunc 触发），视为放弃该快照，重置为未浏览态。
 */
let browsingSnapshot = '';

/**
 * 将指定文本写入输入框 DOM 并同步受控草稿（供上下键浏览历史时「切换问题」填入）。
 * 直接操作用 `textContent` 赋值会丢失光标并重置历史内容（选中全部替换），
 * 符合「切换问题」的语义——整条替换。浏览期间不触发 autoSend。
 */
function writeDraftToInput(text: string): void {
  if (inputDom.value) {
    inputDom.value.textContent = text;
    // 把光标移到末尾，方便用户继续编辑或直接 Enter 发送
    try {
      const range = document.createRange();
      const sel = window.getSelection();
      range.selectNodeContents(inputDom.value);
      range.collapse(false);
      sel?.removeAllRanges();
      sel?.addRange(range);
      inputDom.value.focus();
    } catch {
      /* 忽略光标定位异常，不影响文本写入 */
    }
  }
  draft.value = text;
}

/** ↑ 键：向上浏览更早的历史问题。最早一条已到头则回到当前草稿（browseIndex = -1）。 */
function handleKeyArrowUp(event: KeyboardEvent): void {
  if (props.sending || props.disabled || questionHistory.value.length === 0) return;

  // 首次按 ↑：进入浏览模式，记住当前草稿，从最近一条历史开始
  if (browseIndex.value === -1) {
    browsingSnapshot = draft.value;
    browseIndex.value = questionHistory.value.length - 1;
  } else if (browseIndex.value > 0) {
    // 继续向上：命中更早一条
    browseIndex.value -= 1;
  } else {
    // 已到最早一条（index === 0），再按 ↑ 回到当前草稿
    browseIndex.value = -1;
  }

  writeDraftToInput(browseIndex.value === -1 ? browsingSnapshot : questionHistory.value[browseIndex.value]);
}

/** ↓ 键：向下浏览更新的历史问题。回到最新草稿（browseIndex = -1）后继续 ↓ 不动作。 */
function handleKeyArrowDown(event: KeyboardEvent): void {
  if (props.sending || props.disabled) return;

  if (browseIndex.value === -1) return;
  // 在当前草稿上（browseIndex 已回 -1）：保持现状
  if (browseIndex.value >= questionHistory.value.length - 1) {
    browseIndex.value = -1;
    writeDraftToInput(browsingSnapshot);
    return;
  }
  // 向下移动到更新的历史问题
  browseIndex.value += 1;
  writeDraftToInput(questionHistory.value[browseIndex.value]);
}

/** Escape 键：退出浏览模式，恢复进入浏览前的用户草稿。 */
function handleKeyEscape(event: KeyboardEvent): void {
  if (props.sending || props.disabled) return;
  if (browseIndex.value === -1) return;
  browseIndex.value = -1;
  writeDraftToInput(browsingSnapshot);
}

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

/**
 * 将一条已发送的用户问题存入历史缓存（去重、最多保留最近 MAX_HISTORY 条）。
 * 相同文本只在末尾去重：若已存在则移除旧位置再追加到末尾，保证「最新」语义正确。
 */
function recordQuestion(text: string): void {
  if (isEmpty(text)) return;
  const withoutDup = questionHistory.value.filter((q) => q !== text);
  withoutDup.push(text);
  // 超过上限：丢弃最旧的一条
  if (withoutDup.length > MAX_HISTORY) withoutDup.splice(0, withoutDup.length - MAX_HISTORY);
  questionHistory.value = withoutDup;

  // 发送即退出浏览模式，回到「实时草稿」态（此刻草稿已清空）
  browseIndex.value = -1;
  browsingSnapshot = '';
}

/** 发送：校验→记录历史→清空输入区→向上抛出文本 */
function handleSend(): void {
  if (!sendingAllowed.value || props.sending || props.disabled) return;
  const text = draft.value;
  if (isEmpty(text)) return;

  recordQuestion(text);
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

/**
 * 清空本会话的历史缓存与浏览态（供父组件在会话被删除时调用）。
 *
 * 会话被删除后，其 KeepAlive 缓存槽可能仍驻留内存（仅当删除的是当前激活会话时才
 * 立即释放槽）。若槽继续驻留，本 inputBox 的 `questionHistory` 会随缓存残留，
 * 用户下次手动访问该 sid 时会看到已删除会话的历史。父组件在收到
 * `SESSION_ABORT_STREAM_EVENT` 广播时调用本方法，让历史缓存严格跟随会话删除而清除。
 */
function clearHistory(): void {
  questionHistory.value = [];
  browseIndex.value = -1;
  browsingSnapshot = '';
}

defineExpose({ clearHistory });
</script>

<style lang="scss" scoped>
    @use "sass:math";
    @use "@/common.scss" as common;

    .root{
        height: 100%;
        width: 100%;
        position: relative;
        // 作为父级（h-40 定高 flex 容器）的 flex 项，必须可收缩以保持定高；
        // overflow hidden 复合裁剪，杜绝内容过多时把外层页面顶高。
        display: flex;
        flex-direction: column;
        min-height: 0;
        overflow: hidden;

        >.inputBox{
            // 占据父级剩余高度，min-height:0 允许收缩，配合 overflow-y:auto 实现内部滚动，
            // 内容再多也只滚动，不抬高输入区/页面。
            flex: 1;
            min-height: 0;
            width: 100%;
            box-sizing: border-box;
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

