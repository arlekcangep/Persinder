<script setup lang="ts">
import { reactive } from "vue";

import type { SearchFilterValues } from "@/composables/useSearchFilters";

const props = defineProps<{
  values: SearchFilterValues;
}>();

const emit = defineEmits<{
  apply: [values: SearchFilterValues];
  reset: [];
}>();

const draft = reactive<SearchFilterValues>({ ...props.values });

function apply(): void {
  emit("apply", { ...draft });
}
</script>

<template>
  <section id="filters" class="filters">
    <label class="filter-field">
      <span class="field-label">min_score <em>только семантика</em></span>
      <span class="field-row">
        <input v-model.number="draft.minScore" type="range" min="0.70" max="0.95" step="0.01" />
        <output>{{ draft.minScore.toFixed(2) }}</output>
      </span>
    </label>

    <label class="filter-field">
      <span class="field-label">morph_limit</span>
      <input v-model.number="draft.morphLimit" type="number" min="1" max="100" />
    </label>

    <label class="filter-field">
      <span class="field-label">semantic_limit</span>
      <input v-model.number="draft.semanticLimit" type="number" min="1" max="100" />
    </label>

    <span class="filter-actions">
      <button class="link-btn muted" type="button" @click="emit('reset')">Сбросить</button>
      <button class="link-btn" type="button" @click="apply">Применить</button>
    </span>
  </section>
</template>
