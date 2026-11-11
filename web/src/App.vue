<script setup lang="ts">
import { onBeforeUnmount, onMounted } from "vue";

import AppHeader from "@/components/AppHeader.vue";
import ToastHost from "@/components/ToastHost.vue";
import { useTheme } from "@/composables/useTheme";

useTheme();

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable
  );
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
  if (isTyping(event.target)) return;
  event.preventDefault();
  const input = document.getElementById("global-search");
  if (input instanceof HTMLInputElement) {
    input.focus();
    input.select();
  }
}

onMounted(() => window.addEventListener("keydown", onKeydown));
onBeforeUnmount(() => window.removeEventListener("keydown", onKeydown));
</script>

<template>
  <AppHeader />
  <RouterView />
  <ToastHost />
</template>
