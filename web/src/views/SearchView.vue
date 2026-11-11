<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import ResultRow from "@/components/ResultRow.vue";
import SearchTabs from "@/components/SearchTabs.vue";
import { useLayers } from "@/composables/useLayers";
import { useSearch } from "@/composables/useSearch";
import { useSearchFilters, type SearchFilterValues } from "@/composables/useSearchFilters";
import { useToast } from "@/composables/useToast";
import type { ResultItem } from "@/domain/results";

import FiltersPanel from "@/components/FiltersPanel.vue";

const route = useRoute();
const router = useRouter();
const search = useSearch();
const { filters, apply, reset } = useSearchFilters();
const { layer, counts, visible, total, setLayer } = useLayers();
const { show } = useToast();

const filtersOpen = ref(false);

const EXAMPLES = ["видео", "Немо", "психология"];

const query = computed(() => search.state.query.trim());
const summary = computed(() => {
  if (search.state.error) return "Нет связи с сервером";
  if (search.state.loading) return "Ищем…";
  if (!query.value) return "Морфология и семантика по вашей библиотеке";
  if (total.value === 0) return "Ничего не найдено";

  const parts = [`Результатов: ${total.value}`];
  if (layer.value !== "morph") parts.push(`семантика ${counts.value.semantic}`);
  if (layer.value !== "semantic") parts.push(`морфология ${counts.value.morph}`);
  return parts.join(" · ");
});

const notFoundText = computed(() =>
  layer.value === "morph"
    ? "В морфологическом слое совпадений нет — попробуйте включить семантику."
    : "Попробуйте другое слово или снизьте min_score.",
);

function syncFromRoute(): void {
  const value = typeof route.query.q === "string" ? route.query.q : "";
  if (value === search.state.query) return;
  search.setQuery(value);
  if (value.trim()) void search.runNow();
}

function runExample(text: string): void {
  search.setQuery(text);
  void search.runNow();
}

function applyFilters(values: SearchFilterValues): void {
  // Параметры применяются сразу (на отпускании бегунка) — панель не закрываем.
  apply(values);
  void search.runNow();
}

function resetFilters(): void {
  reset();
  void search.runNow();
}

function openItem(item: ResultItem): void {
  show(`Открыть «${item.title}» — когда сервер начнёт отдавать файлы.`);
}

function revealItem(item: ResultItem): void {
  const target = item.dir ? item.dir.replace(/[\\/]$/, "") : item.title;
  show(`Показать в папке: ${target} — когда сервер начнёт открывать проводник.`);
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && filtersOpen.value) filtersOpen.value = false;
}

onMounted(() => {
  syncFromRoute();
  window.addEventListener("keydown", onKeydown);
});

onBeforeUnmount(() => window.removeEventListener("keydown", onKeydown));

watch(() => route.query.q, syncFromRoute);

watch(
  () => search.state.query,
  (value) => {
    if (route.name !== "search") return;
    const current = typeof route.query.q === "string" ? route.query.q : "";
    if (value === current) return;
    void router.replace({ name: "search", query: value ? { q: value } : {} });
  },
);
</script>

<template>
  <main class="main">
    <SearchTabs
      :model-value="layer"
      :counts="counts"
      :filters-open="filtersOpen"
      @update:model-value="setLayer"
      @toggle-filters="filtersOpen = !filtersOpen"
    />

    <FiltersPanel
      v-if="filtersOpen"
      :values="filters"
      @apply="applyFilters"
      @reset="resetFilters"
    />

    <p class="summary" aria-live="polite">{{ summary }}</p>

    <div v-if="search.state.error" class="state state-error">
      <p class="state-title">Не удалось связаться с сервером</p>
      <p>Проверьте, что запущен <code>persinder serve</code>, и повторите запрос.</p>
      <p><button class="link-btn" type="button" @click="search.runNow()">Повторить</button></p>
    </div>

    <div v-else-if="search.state.loading" aria-hidden="true">
      <div v-for="index in 3" :key="index" class="skeleton">
        <div class="sk-line title"></div>
        <div class="sk-line path"></div>
        <div class="sk-line full"></div>
        <div class="sk-line short"></div>
      </div>
    </div>

    <section v-else-if="!query" class="state">
      <p class="state-title">Введите запрос</p>
      <p>Морфология ищет точные формы слов, семантика — по смыслу.</p>
      <p class="state-examples">
        Например:
        <button
          v-for="example in EXAMPLES"
          :key="example"
          class="link-btn"
          type="button"
          @click="runExample(example)"
        >
          {{ example }}
        </button>
      </p>
    </section>

    <section v-else-if="total === 0" class="state">
      <p class="state-title">Ничего не найдено</p>
      <p>{{ notFoundText }}</p>
    </section>

    <template v-else>
      <section v-if="visible.semantic.length" class="result-group">
        <h2 class="group-title">
          Семантика <span class="group-count">{{ visible.semantic.length }}</span>
        </h2>
        <ResultRow
          v-for="item in visible.semantic"
          :key="item.key"
          :item="item"
          layer="semantic"
          :query="search.state.activeQuery"
          @open="openItem"
          @reveal="revealItem"
        />
      </section>

      <section v-if="visible.morph.length" class="result-group">
        <h2 class="group-title">
          Морфология <span class="group-count">{{ visible.morph.length }}</span>
        </h2>
        <ResultRow
          v-for="item in visible.morph"
          :key="item.key"
          :item="item"
          layer="morph"
          :query="search.state.activeQuery"
          @open="openItem"
          @reveal="revealItem"
        />
      </section>
    </template>
  </main>
</template>
