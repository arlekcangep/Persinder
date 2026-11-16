<script setup lang="ts">
import { useToast } from "@/composables/useToast";

const { message } = useToast();
</script>

<template>
  <div class="toast" :class="{ show: message }" role="status" aria-live="polite">
    {{ message }}
  </div>
</template>
