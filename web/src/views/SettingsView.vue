<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";

import { fetchLogs } from "@/api/client";
import type { ApiLogEvent, ApiLogLevel } from "@/api/types";
import { useTheme } from "@/composables/useTheme";

type Panel = "folders" | "models" | "general" | "logs";

const route = useRoute();
const { theme, setTheme } = useTheme();

const panel = ref<Panel>("folders");

const panels: { id: Panel; label: string }[] = [
  { id: "folders", label: "Папки" },
  { id: "models", label: "Модели" },
  { id: "general", label: "Общие" },
  { id: "logs", label: "Журнал" },
];

/* --- журнал индексации --- */

const events = ref<ApiLogEvent[]>([]);
const logError = ref<string | null>(null);
const loading = ref(false);
let timer: number | null = null;

function levelLabel(level: ApiLogLevel): string {
  const labels: Record<ApiLogLevel, string> = {
    info: "INFO",
    warning: "WARN",
    error: "ERR",
  };
  return labels[level];
}

function formatTime(ts: string): string {
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  const time = date.toLocaleTimeString("ru-RU", { hour12: false });
  const day = date.toLocaleDateString("ru-RU");
  return `${day} ${time}`;
}

async function refreshLogs(): Promise<void> {
  if (loading.value) return;
  loading.value = true;
  logError.value = null;
  try {
    const data = await fetchLogs({ limit: 200 });
    events.value = data.events;
  } catch (error) {
    logError.value = error instanceof Error ? error.message : String(error);
  } finally {
    loading.value = false;
  }
}

watch(panel, (next) => {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
  if (next === "logs") {
    void refreshLogs();
    timer = window.setInterval(() => void refreshLogs(), 5000);
  }
}, { immediate: true });

onBeforeUnmount(() => {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
});
</script>

<template>
  <main class="main settings">
    <div class="settings-head">
      <RouterLink class="back-link" :to="{ name: 'search', query: route.query }">
        ← К поиску
      </RouterLink>
      <h1>Настройки</h1>
    </div>

    <nav class="tabs" aria-label="Разделы настроек">
      <button
        v-for="item in panels"
        :key="item.id"
        class="tab"
        :class="{ active: panel === item.id }"
        type="button"
        :aria-pressed="panel === item.id"
        @click="panel = item.id"
      >
        {{ item.label }}
      </button>
    </nav>

    <section v-if="panel === 'folders'" class="settings-panel">
      <p class="section-label">Папки индексации</p>
      <div class="placeholder">
        <p>
          Список папок сервер читает из <code>persinder.yaml</code> при запуске. Добавление,
          изменение и удаление областей появятся здесь, когда у сервера будет API настроек.
        </p>
        <p>Пока папки правятся в конфиге вручную.</p>
      </div>
      <p class="note">Изменения конфига применяются при перезапуске сервиса.</p>
    </section>

    <section v-else-if="panel === 'models'" class="settings-panel">
      <p class="section-label">Модели эмбеддингов</p>
      <div class="placeholder">
        <p>
          Активную модель сервер пока не отдаёт через API. Посмотреть установленные и
          переключить можно командами <code>persinder model list</code> и
          <code>persinder model use</code>.
        </p>
        <p>Установка и смена модели из интерфейса появятся после серверного API.</p>
      </div>
    </section>

    <section v-else-if="panel === 'general'" class="settings-panel">
      <p class="section-label">Оформление</p>
      <div class="settings-list">
        <div class="settings-row">
          <div class="row-main">
            <div class="row-title">Тема</div>
            <div class="row-sub">светлая или тёмная; выбор запоминается в браузере</div>
          </div>
          <div class="row-actions">
            <label class="switch">
              <input
                type="radio"
                name="theme"
                value="light"
                :checked="theme === 'light'"
                @change="setTheme('light')"
              />
              <span>Светлая</span>
            </label>
            <label class="switch">
              <input
                type="radio"
                name="theme"
                value="dark"
                :checked="theme === 'dark'"
                @change="setTheme('dark')"
              />
              <span>Тёмная</span>
            </label>
          </div>
        </div>
      </div>
      <p class="note">По умолчанию берётся тема системы, пока вы не выберете свою.</p>
    </section>

    <section v-else class="settings-panel">
      <div class="settings-head-row">
        <p class="section-label">Журнал индексации</p>
        <button
          class="tab-action"
          type="button"
          :disabled="loading"
          @click="refreshLogs"
        >
          {{ loading ? "Обновляю…" : "Обновить" }}
        </button>
      </div>
      <p class="note">
        Последние события watcher'а: какие файлы проиндексированы, пропущены или не
        удались. Обновляется автоматически раз в 5 секунд.
      </p>

      <p v-if="logError" class="log-error">{{ logError }}</p>
      <div v-else-if="events.length === 0" class="placeholder">
        <p>
          Событий пока нет. Записи появятся, когда watcher начнёт обрабатывать файлы
          (при запуске <code>persinder serve</code>).
        </p>
      </div>
      <ul v-else class="log-list" aria-label="События индексации">
        <li
          v-for="ev in events"
          :key="ev.id"
          class="log-row"
          :class="`level-${ev.level}`"
        >
          <span class="log-time">{{ formatTime(ev.ts) }}</span>
          <span class="log-level">{{ levelLabel(ev.level) }}</span>
          <span class="log-message">{{ ev.message }}</span>
        </li>
      </ul>
    </section>
  </main>
</template>
