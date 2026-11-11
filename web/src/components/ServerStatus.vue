<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";

import { useServerHealth } from "@/composables/useServerHealth";

import StatsDialog from "./StatsDialog.vue";

const { online, details, check } = useServerHealth();

const open = ref(false);

const address = computed(() => {
  if (details.value?.address) return details.value.address;
  // В разработке страницу отдаёт Vite, а API — отдельный процесс на 8756.
  if (import.meta.env.DEV) return "127.0.0.1:8756";
  return window.location.host;
});

const vectors = computed(() => {
  const value = details.value?.vectors;
  return typeof value === "number" ? value.toLocaleString("ru-RU") : "—";
});

const dotClass = computed(() => {
  if (online.value === null) return "unknown";
  return online.value ? "" : "offline";
});

const label = computed(() => {
  if (online.value === null) return "Проверяем…";
  return online.value ? "Сервер" : "Нет связи";
});

function openStats(): void {
  open.value = true;
  void check();
}

function close(): void {
  open.value = false;
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") close();
}

watch(open, (isOpen) => {
  if (isOpen) window.addEventListener("keydown", onKeydown);
  else window.removeEventListener("keydown", onKeydown);
});

onBeforeUnmount(() => window.removeEventListener("keydown", onKeydown));
</script>

<template>
  <div class="status-wrap">
    <button
      class="status-btn"
      type="button"
      aria-label="Статус сервера"
      aria-describedby="status-tip"
      @click="openStats"
    >
      <span class="dot" :class="dotClass" aria-hidden="true"></span>
      <span>{{ label }}</span>
    </button>
    <span id="status-tip" class="status-tip" role="tooltip">
      {{ address }} · {{ vectors }} векторов
    </span>

    <StatsDialog :open="open" :address="address" @close="close" />
  </div>
</template>
