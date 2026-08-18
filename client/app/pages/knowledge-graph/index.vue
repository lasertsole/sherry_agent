<template>
  <div class="flex flex-col h-full bg-[#f8f9fa] dark:bg-[#131619]">
    <!-- 顶部标题栏 -->
    <div
      class="shrink-0 h-14 flex items-center gap-3 px-4 border-b border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21]">
      <Button
        icon="pi pi-arrow-left"
        text
        rounded
        :aria-label="t('knowledgeGraph.back')"
        @click="goBack" />
      <span class="text-base font-semibold text-gray-900 dark:text-gray-100">
        {{ t('toolbar.knowledgeGraph') }}
      </span>
    </div>

    <!-- 内容占位区 -->
    <div class="flex-1 flex flex-col items-center justify-center gap-4 p-6">
      <i class="pi pi-sitemap text-6xl text-gray-300 dark:text-gray-600" />
      <div class="text-base text-gray-500 dark:text-gray-400">
        {{ t('knowledgeGraph.placeholder') }}
      </div>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { useI18n } from 'vue-i18n';

const { t } = useI18n();
const localePath = useLocalePath();
const router = useRouter();

const goBack = () => {
  router.push(localePath('/home'));
};
</script>
