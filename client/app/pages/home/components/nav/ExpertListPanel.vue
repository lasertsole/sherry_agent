<template>
  <div
    class="expert-list-panel flex flex-col h-full"
    data-test="expert-list-panel">
    <div class="flex items-center justify-between px-3 py-2">
      <span class="font-medium">{{ t('nav.experts') }}</span>
      <Button
        icon="pi pi-plus"
        size="small"
        data-test="expert-create"
        :aria-label="t('experts.create')"
        @click="onCreate" />
    </div>
    <FilterBar
      v-model:keyword="keyword"
      :show="true"
      :keyword-placeholder="t('experts.search')"
      :has-active-filters="keyword.length > 0"
      @clear="keyword = ''" />
    <div class="flex-1 overflow-y-auto px-3">
      <div
        v-if="store.loading"
        class="text-center py-4"
        data-test="expert-loading">
        …
      </div>
      <div
        v-else-if="filteredExperts.length === 0"
        class="text-center py-4 text-gray-400"
        data-test="expert-empty">
        {{ keyword ? t('experts.noMatch') : t('experts.empty') }}
      </div>
      <div
        v-else
        class="space-y-2">
        <ListItemCard
          v-for="expert in filteredExperts"
          :key="expert.name"
          :icon="'pi pi-user'"
          :title="expert.name"
          :badges="
            expert.team_count > 0
              ? [{ value: `${expert.team_count}${t('experts.teamsSuffix')}`, severity: 'info' }]
              : []
          "
          :meta="[
            { icon: 'pi pi-cog', value: expert.model },
            { icon: 'pi pi-bolt', value: `${expert.skills.length} ${t('experts.skillsSuffix')}` }
          ]"
          data-test="expert-card"
          @click="onEdit(expert)" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { useExpertRegistryStore, type ExpertWithUsage } from '@/stores/expert-registry';
import { useRightSidebarStore } from '@/stores/right-sidebar';
import FilterBar from '@/components/FilterBar.vue';
import ListItemCard from '@/components/ListItemCard.vue';

const { t } = useI18n();
const store = useExpertRegistryStore();
const rightSidebar = useRightSidebarStore();
const keyword = ref('');

const filteredExperts = computed(() => {
  const q = keyword.value.trim().toLowerCase();
  if (!q) return store.experts;
  return store.experts.filter(
    expert =>
      expert.name.toLowerCase().includes(q) ||
      expert.description.toLowerCase().includes(q) ||
      expert.model.toLowerCase().includes(q)
  );
});

function onEdit(expert: ExpertWithUsage): void {
  rightSidebar.openTab('expertEdit', { expertName: expert.name });
}
function onCreate(): void {
  rightSidebar.openTab('expertEdit', { expertName: null });
}

onMounted(() => {
  void store.loadAll();
});
</script>
