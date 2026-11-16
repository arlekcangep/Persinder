<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

import { useServerHealth } from "@/composables/useServerHealth";

const props = defineProps<{
  open: boolean;
  address: string;
}>();

const emit = defineEmits<{ close: [] }>();

const { online, details } = useServerHealth();

const closeButton = ref<HTMLButtonElement | null>(null);
let restoreFocus: HTMLElement | null = null;

function formatNumber(value: number | undefined): string {
  return typeof value === "number" ? value.toLocaleString("ru-RU") : "—";
}

function withUnit(value: number | undefined, one: string, few: string, many: string): string {
  if (typeof value !== "number") return "—";
  const mod10 = value % 10;
  const mod100 = value % 100;
  const word =
    mod10 === 1 && mod100 !== 11
      ? one
      : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)
        ? few
        : many;
  return `${formatNumber(value)} ${word}`;
}

const rows = computed(() => [
  { label: "Сервер", value: props.address },
  {
    label: "Состояние",
    value: online.value === null ? "проверяем…" : online.value ? "работает" : "нет связи",
  },
  { label: "Документов", value: formatNumber(details.value?.documents) },
  { label: "Векторов", value: formatNumber(details.value?.vectors) },
  { label: "Модель поиска", value: details.value?.model ?? "—" },
  { label: "Эмбеддинги", value: details.value?.embedding ?? "—" },
  {
    label: "Очередь эмбеддингов",
    value: withUnit(details.value?.pending_embeddings, "задача", "задачи", "задач"),
  },
]);

const partial = computed(
  () => details.value?.documents === undefined || details.value?.vectors === undefined,
);

watch(
  () => props.open,
  async (isOpen) => {
    if (isOpen) {
      restoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      await nextTick();
      closeButton.value?.focus();
    } else {
      restoreFocus?.focus();
      restoreFocus = null;
    }
  },
);
</script>

<template>
  <div v-if="open" class="modal">
    <div class="modal-backdrop" @click="emit('close')"></div>
    <div class="modal-dialog" role="dialog" aria-modal="true" aria-labelledby="stats-title">
      <div class="modal-head">
        <h2 id="stats-title">Статистика</h2>
        <button
          ref="closeButton"
          class="modal-close"
          type="button"
          aria-label="Закрыть"
          @click="emit('close')"
        >
          ×
        </button>
      </div>
      <dl class="stats-list">
        <div v-for="row in rows" :key="row.label">
          <dt>{{ row.label }}</dt>
          <dd>{{ row.value }}</dd>
        </div>
      </dl>
      <p v-if="partial" class="note">
        Документы, векторы и модель появятся, когда сервер начнёт отдавать статистику.
      </p>
    </div>
  </div>
</template>
