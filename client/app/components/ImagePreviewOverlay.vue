<template>
  <Teleport to="body">
    <Transition name="image-preview-fade">
      <div
        v-if="isPreviewVisible"
        ref="overlayRef"
        class="image-preview-overlay"
        tabindex="-1"
        role="dialog"
        aria-modal="true"
        :aria-label="t('title')"
        @click="closePreview"
        @keydown.esc="closePreview"
        @keydown.tab.prevent="trapTab">
        <img
          :src="previewSrc"
          class="image-preview-img"
          :style="{ transform: `scale(${scale}) translate(${translateX}px, ${translateY}px)` }"
          @click.stop
          @wheel.prevent="onWheel"
          @mousedown="onMouseDown"
          @load="onImageLoad" />
        <div class="image-preview-hint">{{ t('hint') }}</div>
        <button
          type="button"
          ref="closeBtnRef"
          class="image-preview-close"
          :aria-label="t('close')"
          @click="closePreview"><i class="pi pi-times"></i></button>
      </div>
    </Transition>
  </Teleport>
</template>

<i18n lang="json">
{
  "en": {
    "hint": "Scroll to zoom · Drag to move · ESC or click blank to close",
    "title": "Image preview",
    "close": "Close"
  },
  "ja": {
    "hint": "スクロールでズーム ・ ドラッグで移動 ・ ESC または空白クリックで閉じる",
    "title": "画像プレビュー",
    "close": "閉じる"
  },
  "ko": {
    "hint": "스크롤로 확대 · 드래그로 이동 · ESC 또는 빈 곳 클릭으로 닫기",
    "title": "이미지 미리보기",
    "close": "닫기"
  },
  "zh": {
    "hint": "滚动缩放 · 拖拽移动 · ESC 或点击空白关闭",
    "title": "图片预览",
    "close": "关闭"
  }
}
</i18n>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';

const { isPreviewVisible, previewSrc, closePreview } = useImagePreview();

const { t } = useI18n({ useScope: 'local' });

/** 覆盖层与关闭按钮的 DOM 引用（用于焦点管理 / Tab 陷阱） */
const overlayRef = ref<HTMLElement | null>(null);
const closeBtnRef = ref<HTMLButtonElement | null>(null);
/** 打开预览前持有焦点的元素，关闭时恢复焦点 */
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

const onImageLoad = () => {
  resetTransform();
};

const onWheel = (e: WheelEvent) => {
  const delta = e.deltaY > 0 ? -0.1 : 0.1;
  scale.value = Math.min(5, Math.max(0.2, scale.value + delta));
};

const onMouseDown = (e: MouseEvent) => {
  isDragging.value = true;
  dragStartX.value = e.clientX;
  dragStartY.value = e.clientY;
  dragStartTx.value = translateX.value;
  dragStartTy.value = translateY.value;
  document.addEventListener('mousemove', onMouseMove);
  document.addEventListener('mouseup', onMouseUp);
};

const onMouseMove = (e: MouseEvent) => {
  if (!isDragging.value) return;
  translateX.value = dragStartTx.value + (e.clientX - dragStartX.value) / scale.value;
  translateY.value = dragStartTy.value + (e.clientY - dragStartY.value) / scale.value;
};

const onMouseUp = () => {
  isDragging.value = false;
  document.removeEventListener('mousemove', onMouseMove);
  document.removeEventListener('mouseup', onMouseUp);
};

/** Tab 陷阱：覆盖层内仅有两个焦点停靠点（覆盖层自身 + 关闭按钮），循环切换 */
const trapTab = () => {
  if (document.activeElement === overlayRef.value) {
    closeBtnRef.value?.focus();
  } else {
    overlayRef.value?.focus();
  }
};

watch(isPreviewVisible, async (visible) => {
  if (visible) {
    // 打开时记录来源焦点，并把焦点移入覆盖层，使 ESC / Tab 立即生效
    previouslyFocusedEl = document.activeElement as HTMLElement | null;
    await nextTick();
    overlayRef.value?.focus();
  } else {
    // 关闭时恢复焦点到来源元素，避免焦点丢失回 body
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
.image-preview-overlay {
  position: fixed;
  inset: 0;
  z-index: 9999;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.8);
  cursor: zoom-out;
}

.image-preview-img {
  max-width: 90vw;
  max-height: 85vh;
  object-fit: contain;
  cursor: grab;
  user-select: none;
  -webkit-user-drag: none;
  transition: transform 0.05s ease-out;

  &:active {
    cursor: grabbing;
  }
}

.image-preview-hint {
  position: absolute;
  bottom: 1.5rem;
  color: rgba(255, 255, 255, 0.6);
  font-size: 0.75rem;
  pointer-events: none;
}

/* 右上角关闭按钮：半透明白色圆形底，悬停增亮 */
.image-preview-close {
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

.image-preview-fade-enter-active,
.image-preview-fade-leave-active {
  transition: opacity 0.2s ease;
}

.image-preview-fade-enter-from,
.image-preview-fade-leave-to {
  opacity: 0;
}
</style>
