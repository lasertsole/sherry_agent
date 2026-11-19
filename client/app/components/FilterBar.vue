<template>
  <div
    v-if="show"
    class="px-3 pb-2 flex flex-col gap-2"
    data-test="filter-bar">
    <InputText
      :model-value="keyword"
      :placeholder="keywordPlaceholder"
      class="w-full text-sm"
      data-test="filter-bar-keyword"
      @update:model-value="$emit('update:keyword', String($event ?? ''))" />
    <slot name="extra-filters" />
    <Button
      v-if="hasActiveFilters"
      icon="pi pi-filter-slash"
      size="small"
      text
      data-test="filter-bar-clear"
      @click="$emit('clear')" />
  </div>
</template>

<script setup lang="ts">
defineProps<{
  show: boolean;
  keyword?: string;
  keywordPlaceholder?: string;
  hasActiveFilters?: boolean;
}>();
defineEmits<{ 'update:keyword': [value: string]; clear: [] }>();
</script>
