<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { useSearch } from "@/composables/useSearch";
import { useTheme } from "@/composables/useTheme";

import ServerStatus from "./ServerStatus.vue";

const route = useRoute();
const router = useRouter();
const search = useSearch();
const { theme, toggle } = useTheme();

const input = ref<HTMLInputElement | null>(null);

const settingsActive = computed(() => route.name === "settings");
const themeLabel = computed(() => (theme.value === "dark" ? "Светлая тема" : "Тёмная тема"));

function onInput(event: Event): void {
  search.setQuery((event.target as HTMLInputElement).value);
}

function onSubmit(): void {
  void search.runNow();
  if (route.name !== "search") {
    void router.push({
      name: "search",
      query: search.state.query ? { q: search.state.query } : {},
    });
  }
  input.value?.blur();
}

function clear(): void {
  search.clear();
  input.value?.focus();
}
</script>

<template>
  <header class="header">
    <div class="header-inner">
      <RouterLink class="logo" :to="{ name: 'search' }" aria-label="Persinder — к поиску">
        Persinder
      </RouterLink>

      <form class="search-form" role="search" @submit.prevent="onSubmit">
        <svg class="search-icon" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="11" cy="11" r="7"></circle>
          <path d="m20 20-3.5-3.5"></path>
        </svg>
        <input
          id="global-search"
          ref="input"
          name="q"
          type="search"
          :value="search.state.query"
          placeholder="Слово, фраза или вопрос"
          autocomplete="off"
          spellcheck="false"
          aria-label="Поисковый запрос"
          @input="onInput"
          @keydown.esc.prevent="clear"
        />
        <button
          v-if="search.state.query"
          class="clear-btn"
          type="button"
          aria-label="Очистить запрос"
          title="Очистить"
          @click="clear"
        >
          ×
        </button>
        <button class="search-submit" type="submit">Найти</button>
      </form>

      <div class="header-actions">
        <ServerStatus />

        <button
          class="icon-btn"
          type="button"
          :aria-label="themeLabel"
          :title="themeLabel"
          @click="toggle"
        >
          <svg v-if="theme === 'light'" class="icon-moon" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5Z"></path>
          </svg>
          <svg v-else class="icon-sun" viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="12" r="4"></circle>
            <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"></path>
          </svg>
        </button>

        <RouterLink
          class="icon-btn"
          :class="{ 'is-active': settingsActive }"
          :to="{ name: 'settings', query: route.query }"
          aria-label="Настройки"
          title="Настройки"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="12" r="3"></circle>
            <path
              d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4v-.2a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"
            ></path>
          </svg>
        </RouterLink>
      </div>
    </div>
  </header>
</template>
