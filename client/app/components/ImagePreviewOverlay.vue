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
        @keydown.left.prevent="galleryItems.length > 1 && prevImage()"
        @keydown.right.prevent="galleryItems.length > 1 && nextImage()"
        @keydown.tab.prevent="trapTab">
        <button
          v-if="galleryItems.length > 1"
          type="button"
          ref="prevBtnRef"
          class="image-preview-nav image-preview-prev"
          :aria-label="t('previous')"
          @click.stop="prevImage">
          <i class="pi pi-chevron-left"></i>
        </button>
        <img
          :src="previewSrc"
          class="image-preview-img"
          :style="{ transform: `scale(${scale}) translate(${translateX}px, ${translateY}px)` }"
          @click.stop
          @wheel.prevent="onWheel"
          @mousedown="onMouseDown"
          @load="onImageLoad" />
        <button
          v-if="galleryItems.length > 1"
          type="button"
          ref="nextBtnRef"
          class="image-preview-nav image-preview-next"
          :aria-label="t('next')"
          @click.stop="nextImage">
          <i class="pi pi-chevron-right"></i>
        </button>
        <div
          v-if="galleryItems.length > 1"
          class="image-preview-counter">
          {{ currentIndex + 1 }} / {{ galleryItems.length }}
        </div>
        <div class="image-preview-hint">{{ t('hint') }}</div>
        <button
          type="button"
          ref="closeBtnRef"
          class="image-preview-close"
          :aria-label="t('close')"
          @click="closePreview">
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
    "title": "Image preview",
    "close": "Close",
    "previous": "Previous image",
    "next": "Next image"
  },
  "ja": {
    "hint": "スクロールでズーム ・ ドラッグで移動 ・ ESC または空白クリックで閉じる",
    "title": "画像プレビュー",
    "close": "閉じる",
    "previous": "前の画像",
    "next": "次の画像"
  },
  "ko": {
    "hint": "스크롤로 확대 · 드래그로 이동 · ESC 또는 빈 곳 클릭으로 닫기",
    "title": "이미지 미리보기",
    "close": "닫기",
    "previous": "이전 이미지",
    "next": "다음 이미지"
  },
  "zh": {
    "hint": "滚动缩放 · 拖拽移动 · ESC 或点击空白关闭",
    "title": "图片预览",
    "close": "关闭",
    "previous": "上一张",
    "next": "下一张"
  }
}
</i18n>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';

const { isPreviewVisible, previewSrc, galleryItems, currentIndex, closePreview, nextImage, prevImage } =
  useImagePreview();

const { t } = useI18n({ useScope: 'local' });

/** DOM references for the overlay and its controls (focus management / Tab trap) */
const overlayRef = ref<HTMLElement | null>(null);
const closeBtnRef = ref<HTMLButtonElement | null>(null);
const prevBtnRef = ref<HTMLButtonElement | null>(null);
const nextBtnRef = ref<HTMLButtonElement | null>(null);
/** The element that held focus before the preview opened; focus is restored to it on close */
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

/** Tab trap: cycle through the overlay and its visible controls, in DOM order. */
const trapTab = () => {
  const stops = [overlayRef.value, prevBtnRef.value, nextBtnRef.value, closeBtnRef.value].filter(
    (element): element is HTMLElement => element !== null
  );
  if (stops.length === 0) return;
  const active = document.activeElement as HTMLElement | null;
  const index = stops.indexOf(active as HTMLElement);
  stops[(index + 1) % stops.length]?.focus();
};

// A gallery step swaps the image: the zoom/pan of the previous frame must not
// carry over to the next one.
watch(previewSrc, resetTransform);

watch(isPreviewVisible, async visible => {
  if (visible) {
    // Record the source focus when opening and move focus into the overlay so ESC / Tab work immediately
    previouslyFocusedEl = document.activeElement as HTMLElement | null;
    await nextTick();
    overlayRef.value?.focus();
  } else {
    // Restore focus to the source element on close, avoiding focus falling back to body
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

/* Close button in the top-right corner: translucent white round base, brightens on hover */
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

/* Previous / next controls: same translucent round base as the close button. */
.image-preview-nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 3rem;
  height: 3rem;
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

.image-preview-prev {
  left: 1.5rem;
}

.image-preview-next {
  right: 1.5rem;
}

.image-preview-counter {
  position: absolute;
  top: 1.25rem;
  left: 50%;
  transform: translateX(-50%);
  padding: 0.25rem 0.75rem;
  border-radius: 9999px;
  background: rgba(0, 0, 0, 0.35);
  color: #ffffff;
  font-size: 0.875rem;
  pointer-events: none;
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
