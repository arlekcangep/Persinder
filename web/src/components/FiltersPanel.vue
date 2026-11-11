<script setup lang="ts">
import { reactive, watch } from "vue";

import type { SearchFilterValues } from "@/composables/useSearchFilters";

const props = defineProps<{
  values: SearchFilterValues;
}>();

const emit = defineEmits<{
  apply: [values: SearchFilterValues];
  reset: [];
}>();

const draft = reactive<SearchFilterValues>({ ...props.values });

watch(
  () => props.values,
  (next) => Object.assign(draft, next),
  { deep: true },
);

function apply(): void {
  emit("apply", { ...draft });
}
</script>

<template>
  <section id="filters" class="filters">
    <label class="filter-field">
      <span class="field-label">min_score <em>только семантика</em></span>
      <span class="field-row">
        <!-- change на range срабатывает, когда юзер отпустил бегунок -->
        <input
          v-model.number="draft.minScore"
          type="range"
          min="0.70"
          max="0.95"
          step="0.01"
          @change="apply"
        />
        <output>{{ draft.minScore.toFixed(2) }}</output>
      </span>
    </label>

    <label class="filter-field">
      <span class="field-label">morph_limit</span>
      <input
        v-model.number="draft.morphLimit"
        type="number"
        min="1"
        max="100"
        @change="apply"
      />
    </label>

    <label class="filter-field">
      <span class="field-label">semantic_limit</span>
      <input
        v-model.number="draft.semanticLimit"
        type="number"
        min="1"
        max="100"
        @change="apply"
      />
    </label>

    <span class="filter-actions">
      <button class="link-btn muted" type="button" @click="emit('reset')">Сбросить</button>
    </span>
  </section>
</template>
