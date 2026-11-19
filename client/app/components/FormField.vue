<template>
  <div
    class="flex flex-col gap-1"
    data-test="form-field">
    <label
      v-if="label"
      class="text-sm font-medium text-[#111827] dark:text-[#E5E7EB]"
      data-test="form-field-label">
      {{ label }}
    </label>
    <slot />
    <span
      v-if="hint"
      class="text-xs text-gray-400"
      data-test="form-field-hint"
      >{{ hint }}</span
    >
  </div>
</template>

<script setup lang="ts">
interface Props {
  /** Field label rendered above the slotted control */
  label?: string;
  /** Optional helper text rendered below the slotted control */
  hint?: string;
}
defineProps<Props>();
</script>
