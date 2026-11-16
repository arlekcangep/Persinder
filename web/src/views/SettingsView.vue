<script setup lang="ts">
import { ref } from "vue";
import { useRoute } from "vue-router";

import { useTheme } from "@/composables/useTheme";

type Panel = "folders" | "models" | "general";

const route = useRoute();
const { theme, setTheme } = useTheme();

const panel = ref<Panel>("folders");

const panels: { id: Panel; label: string }[] = [
  { id: "folders", label: "Папки" },
  { id: "models", label: "Модели" },
  { id: "general", label: "Общие" },
];
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

    <section v-else class="settings-panel">
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
  </main>
</template>
