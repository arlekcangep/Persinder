<script setup lang="ts">
import { computed } from "vue";

import type { ResultLayer } from "@/domain/layers";
import { splitHighlight, type ResultItem } from "@/domain/results";

const props = defineProps<{
  item: ResultItem;
  layer: ResultLayer;
  query: string;
}>();

const emit = defineEmits<{
  open: [item: ResultItem];
  reveal: [item: ResultItem];
}>();

const isSemantic = computed(() => props.layer === "semantic");
// bm25 в SQLite отрицательный (меньше — лучше); показываем модуль, как в эскизе.
const score = computed(
  () =>
    `${isSemantic.value ? "cos" : "bm25"} ${Math.abs(props.item.score).toFixed(4)}`,
);
const snippet = computed(() => splitHighlight(props.item.snippet, props.query));
</script>

<template>
  <article class="result">
    <h3 class="result-title">
      <a href="#" @click.prevent="emit('open', item)">{{ item.title }}</a>
    </h3>

    <p class="result-path">
      <template v-if="item.dir">
        {{ item.dir }}<span class="sep">›</span>{{ item.filename }}
      </template>
      <template v-else>{{ item.filename }}</template>
      <span class="sep">·</span><span class="area">{{ item.areaLabel }}</span>
    </p>

    <p v-if="item.snippet" class="result-snippet">
      <template v-for="(part, index) in snippet" :key="index">
        <b v-if="part.match">{{ part.text }}</b>
        <template v-else>{{ part.text }}</template>
      </template>
    </p>
    <p v-else-if="item.snippetLoading" class="result-snippet result-snippet-loading">
      Контекст загружается…
    </p>

    <div class="result-meta">
      <span class="score">{{ score }}</span>
      <span>фрагмент {{ item.chunkIndex + 1 }}</span>
      <span class="result-actions">
        <button class="link-btn" type="button" @click="emit('open', item)">Открыть</button>
        <button class="link-btn muted" type="button" @click="emit('reveal', item)">
          Показать в папке
        </button>
      </span>
    </div>
  </article>
</template>
