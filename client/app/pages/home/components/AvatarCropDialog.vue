<template>
  <Dialog
    v-model:visible="visible"
    :header="dialogTitle"
    :modal="true"
    :closable="true"
    class="w-[94vw] max-w-[95vw]"
    @hide="destroyCropper">
    <div class="flex flex-col gap-3">
      <div class="flex gap-3">
        <div
          ref="imageContainer"
          class="relative h-[520px] w-full min-w-0 flex-1 overflow-hidden rounded-lg border border-gray-300 dark:border-gray-700">
          <img
            ref="cropImage"
            :src="src"
            alt="avatar to crop"
            style="display: block; max-width: 100%" />
        </div>
        <div class="flex w-[22%] shrink flex-col gap-2 self-start">
          <span class="text-xs font-medium text-gray-500 dark:text-gray-400">
            {{ t('config.crop.preview') }}
          </span>
          <div
            class="w-full overflow-hidden rounded-lg border border-gray-300 bg-black/10 dark:border-gray-700"
            :style="{ aspectRatio: String(props.aspectRatio) }">
            <canvas ref="previewCanvas" class="block h-full w-full" />
          </div>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <Button
          icon="pi pi-plus"
          text
          severity="secondary"
          :aria-label="t('config.crop.zoomIn')"
          @click="zoomBy(0.1)" />
        <Button
          icon="pi pi-minus"
          text
          severity="secondary"
          :aria-label="t('config.crop.zoomOut')"
          @click="zoomBy(-0.1)" />
        <Button
          icon="pi pi-sync"
          text
          severity="secondary"
          :aria-label="t('config.crop.rotate')"
          @click="rotateBy(90)" />
        <div class="flex-1" />
        <Button
          :label="t('config.cancel')"
          icon="pi pi-times"
          severity="secondary"
          @click="visible = false" />
        <Button
          :label="t('config.crop.confirm')"
          icon="pi pi-check"
          :disabled="!cropper"
          @click="confirmCrop" />
      </div>
    </div>
    <template #footer />
  </Dialog>
</template>

<script lang="ts" setup>
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import Cropper from 'cropperjs';
import 'cropperjs/dist/cropper.css';

const { t } = useI18n();

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    src: string;
    /** 裁剪框宽高比（如 1 = 正方形；背景为当前电脑屏幕长宽比）。默认正方形 */
    aspectRatio?: number;
    /** 裁剪输出尺寸（背景图等比铺满窗口，需足够大；avatar 固定 512） */
    outputWidth?: number;
    outputHeight?: number;
    /** 对话框标题，用于区分裁剪用途 */
    header?: string;
  }>(),
  { aspectRatio: 1, outputWidth: 512, outputHeight: 512, header: '' }
);
const emits = defineEmits<{ 'update:modelValue': [value: boolean]; cropped: [dataUrl: string] }>();

const dialogTitle = computed(() => props.header || '裁剪图片');

const visible = ref(props.modelValue);
watch(
  () => props.modelValue,
  v => (visible.value = v)
);
watch(visible, v => emits('update:modelValue', v));

const imageContainer = ref<HTMLElement>();
const cropImage = ref<HTMLImageElement>();
const previewCanvas = ref<HTMLCanvasElement>();
const cropper = ref<Cropper | null>(null);

/** 预览重绘的 requestAnimationFrame 句柄，拖动时避免每帧同步绘制 */
let previewFrame = 0;
const schedulePreview = () => {
  if (previewFrame || !cropper.value) return;
  previewFrame = requestAnimationFrame(() => {
    previewFrame = 0;
    renderPreview();
  });
};

/** 实时渲染裁剪结果预览，缩小输出以保持拖拽流畅 */
const renderPreview = () => {
  const cropperInstance = cropper.value;
  const canvas = previewCanvas.value;
  if (!cropperInstance || !canvas) return;
  const scale = Math.min(1, 320 / Math.max(props.outputWidth, 1));
  const canvasData = cropperInstance.getCroppedCanvas({
    width: Math.max(1, Math.round(props.outputWidth * scale)),
    height: Math.max(1, Math.round(props.outputHeight * scale)),
    imageSmoothingEnabled: true,
    imageSmoothingQuality: 'medium'
  });
  canvas.width = canvasData.width;
  canvas.height = canvasData.height;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(canvasData, 0, 0);
};

watch(
  [visible, () => props.src],
  ([v, src]) => {
    if (!v || !src) return;
    // 等图片渲染完毕再初始化 cropper
    setTimeout(() => initCropper(), 0);
  },
  { immediate: true }
);

const initCropper = () => {
  destroyCropper();
  if (!cropImage.value || !imageContainer.value) return;
  cropper.value = new Cropper(cropImage.value, {
    viewMode: 1,
    dragMode: 'move',
      aspectRatio: props.aspectRatio, // 裁剪比例：头像 1:1 正方形；背景 当前电脑屏幕长宽比
    autoCropArea: 0.8,
    background: false,
    modal: true,
    guides: true,
    center: true,
    highlight: false,
    cropBoxMovable: true,
    cropBoxResizable: false, // 固定大小，只能移动/缩放图片
    toggleDragModeOnDblclick: false,
    // 移动 / 缩放 / 旋转 / 裁剪框变化时实时刷新结果预览
    move: schedulePreview,
    zoom: schedulePreview,
    rotate: schedulePreview,
    crop: schedulePreview
  });
  schedulePreview();
};

const zoomBy = (ratio: number) => {
  cropper.value?.zoom(ratio);
};

const rotateBy = (deg: number) => {
  cropper.value?.rotate(deg);
};

const confirmCrop = () => {
  if (!cropper.value) return;
  const canvas = cropper.value.getCroppedCanvas({
    width: props.outputWidth,
    height: props.outputHeight,
    imageSmoothingEnabled: true,
    imageSmoothingQuality: 'high'
  });
  emits('cropped', canvas.toDataURL('image/png'));
  visible.value = false;
};

const destroyCropper = () => {
  cropper.value?.destroy();
  cropper.value = null;
  if (previewFrame) {
    cancelAnimationFrame(previewFrame);
    previewFrame = 0;
  }
  const canvas = previewCanvas.value;
  if (canvas) {
    canvas.width = 0;
    canvas.height = 0;
  }
};
</script>

<style>
/* cropperjs 需要父容器尺寸确定，imageContainer 已固定 520px 高 */
</style>
