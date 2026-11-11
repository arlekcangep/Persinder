<script setup lang="ts">
import type { LayerCounts, LayerFilter } from "@/domain/layers";

const props = defineProps<{
  modelValue: LayerFilter;
  counts: LayerCounts;
  filtersOpen: boolean;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: LayerFilter];
  "toggle-filters": [];
}>();

const tabs: { id: LayerFilter; label: string; count: keyof LayerCounts }[] = [
  { id: "both", label: "Все", count: "all" },
  { id: "semantic", label: "Семантика", count: "semantic" },
  { id: "morph", label: "Морфология", count: "morph" },
];

function select(id: LayerFilter): void {
  if (id !== props.modelValue) emit("update:modelValue", id);
}
</script>

<template>
  <nav class="tabs" aria-label="Слои поиска">
    <button
      v-for="tab in tabs"
      :key="tab.id"
      class="tab"
      :class="{ active: modelValue === tab.id }"
      type="button"
      :aria-pressed="modelValue === tab.id"
      @click="select(tab.id)"
    >
      {{ tab.label }} <span>{{ counts[tab.count] }}</span>
    </button>

    <button
      class="tab-action"
      type="button"
      aria-controls="filters"
      :aria-expanded="filtersOpen"
      @click="emit('toggle-filters')"
    >
      Параметры
    </button>
  </nav>
</template>
