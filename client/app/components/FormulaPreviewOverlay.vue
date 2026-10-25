<template>
  <Teleport to="body">
    <Transition name="formula-preview-fade">
      <div
        v-if="isFormulaPreviewVisible"
        ref="overlayRef"
        class="formula-preview-overlay"
        tabindex="-1"
        role="dialog"
        aria-modal="true"
        :aria-label="t('title')"
        @click="closeFormulaPreview"
        @keydown.esc="closeFormulaPreview"
        @keydown.tab.prevent="trapTab">
        <div
          class="formula-preview-content"
          :style="{ transform: `scale(${scale}) translate(${translateX}px, ${translateY}px)` }"
          @click.stop
          @wheel.prevent="onWheel"
          @mousedown="onMouseDown">
          <!-- The source is the already-sanitized KaTeX fragment from the chat;
               .fragment applies the same DOMPurify allowlist again, without a
               markdown pass (no formula re-parsing, no click marks). -->
          <div v-safe-html.fragment="previewHtml"></div>
        </div>
        <div class="formula-preview-hint">{{ t('hint') }}</div>
        <button
          type="button"
          ref="closeBtnRef"
          class="formula-preview-close"
          :aria-label="t('close')"
          @click.stop="closeFormulaPreview">
          <i class="pi pi-times"></i>
        </button>
      </div>
    </Transition>
  </Teleport>
</template>

<i18n lang="json">
{
  "en": {
    "hint": "Scroll to zoom · Drag to move · ESC or click blank to close",
    "title": "Formula preview",
    "close": "Close"
  },
  "ja": {
    "hint": "スクロールでズーム ・ ドラッグで移動 ・ ESC または空白クリックで閉じる",
    "title": "数式プレビュー",
    "close": "閉じる"
  },
  "ko": {
    "hint": "스크롤로 확대 · 드래그로 이동 · ESC 또는 빈 곳 클릭으로 닫기",
    "title": "수식 미리보기",
    "close": "닫기"
  },
  "zh": {
    "hint": "滚动缩放 · 拖拽移动 · ESC 或点击空白关闭",
    "title": "公式预览",
    "close": "关闭"
  }
}
</i18n>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';
/* eslint-disable-next-line @typescript-eslint/no-restricted-imports */
import { useFormulaPreview } from '~/composables/use-formula-preview';

const { previewHtml, isFormulaPreviewVisible, closeFormulaPreview } = useFormulaPreview();
const { t } = useI18n({ useScope: 'local' });

const overlayRef = ref<HTMLElement | null>(null);
const closeBtnRef = ref<HTMLButtonElement | null>(null);
let previouslyFocusedEl: HTMLElement | null = null;

const scale = ref(1);
const translateX = ref(0);
const translateY = ref(0);
const isDragging = ref(false);
const dragStartX = ref(0);
const dragStartY = ref(0);
const dragStartTx = ref(0);
const dragStartTy = ref(0);

const resetTransform = () => {
  scale.value = 1;
  translateX.value = 0;
  translateY.value = 0;
};

const onWheel = (event: WheelEvent) => {
  const delta = event.deltaY > 0 ? -0.1 : 0.1;
  scale.value = Math.min(5, Math.max(0.4, scale.value + delta));
};

const onMouseDown = (event: MouseEvent) => {
  isDragging.value = true;
  dragStartX.value = event.clientX;
  dragStartY.value = event.clientY;
  dragStartTx.value = translateX.value;
  dragStartTy.value = translateY.value;
  document.addEventListener('mousemove', onMouseMove);
  document.addEventListener('mouseup', onMouseUp);
};

const onMouseMove = (event: MouseEvent) => {
  if (!isDragging.value) return;
  translateX.value = dragStartTx.value + (event.clientX - dragStartX.value) / scale.value;
  translateY.value = dragStartTy.value + (event.clientY - dragStartY.value) / scale.value;
};

const onMouseUp = () => {
  isDragging.value = false;
  document.removeEventListener('mousemove', onMouseMove);
  document.removeEventListener('mouseup', onMouseUp);
};

const trapTab = () => {
  if (document.activeElement === overlayRef.value) {
    closeBtnRef.value?.focus();
  } else {
    overlayRef.value?.focus();
  }
};

watch(isFormulaPreviewVisible, async visible => {
  if (visible) {
    previouslyFocusedEl = document.activeElement as HTMLElement | null;
    resetTransform();
    await nextTick();
    overlayRef.value?.focus();
  } else {
    previouslyFocusedEl?.focus();
    previouslyFocusedEl = null;
    resetTransform();
    document.removeEventListener('mousemove', onMouseMove);
    document.removeEventListener('mouseup', onMouseUp);
  }
});

onUnmounted(() => {
  document.removeEventListener('mousemove', onMouseMove);
  document.removeEventListener('mouseup', onMouseUp);
});
</script>

<style lang="scss" scoped>
.formula-preview-overlay {
  position: fixed;
  inset: 0;
  z-index: 9999;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.82);
  cursor: zoom-out;
}

.formula-preview-content {
  max-width: 90vw;
  max-height: 85vh;
  overflow: auto;
  padding: 2rem 2.5rem;
  border-radius: 0.75rem;
  background: #ffffff;
  color: #111827;
  font-size: clamp(1.75rem, 4vw, 3.5rem);
  line-height: 1.4;
  cursor: grab;
  user-select: none;
  transition: transform 0.05s ease-out;

  &:active {
    cursor: grabbing;
  }
}

.dark .formula-preview-content {
  background: #111827;
  color: #f3f4f6;
}

.formula-preview-hint {
  position: absolute;
  bottom: 1.5rem;
  color: rgba(255, 255, 255, 0.6);
  font-size: 0.75rem;
  pointer-events: none;
}

.formula-preview-close {
  position: absolute;
  top: 1rem;
  right: 1rem;
  width: 2.5rem;
  height: 2.5rem;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.15);
  color: #ffffff;
  cursor: pointer;
  transition: background 0.2s ease;

  &:hover {
    background: rgba(255, 255, 255, 0.3);
  }
}

.formula-preview-fade-enter-active,
.formula-preview-fade-leave-active {
  transition: opacity 0.2s ease;
}

.formula-preview-fade-enter-from,
.formula-preview-fade-leave-to {
  opacity: 0;
}
</style>
