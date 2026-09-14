<template>
  <!-- Fallback shown while a lazily-loaded dialog chunk is being fetched (see
       defineAsyncComponent in home/index.vue). Deliberately lightweight and
       token-consistent with the existing loading overlays (SubagentFlowGraph,
       StatsDialog): centered spinner on a translucent surface, announced to
       assistive tech. -->
  <div
    class="fixed inset-0 z-50 flex items-center justify-center bg-white/60 dark:bg-[#131619]/60"
    role="status"
    aria-live="polite">
    <i
      class="pi pi-spin pi-spinner text-3xl text-gray-400 dark:text-gray-500"
      aria-hidden="true" />
  </div>
</template>
