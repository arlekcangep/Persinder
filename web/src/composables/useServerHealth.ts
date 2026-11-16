import { ref } from "vue";

import { health } from "@/api/client";
import type { ApiHealthResponse } from "@/api/types";

const online = ref<boolean | null>(null);
const lastChecked = ref<Date | null>(null);
const details = ref<ApiHealthResponse | null>(null);

let timer: number | null = null;
let inFlight = false;

async function check(): Promise<void> {
  if (inFlight) return;
  inFlight = true;
  try {
    const response = await health();
    details.value = response;
    online.value = response.status === "ok";
  } catch {
    details.value = null;
    online.value = false;
  } finally {
    lastChecked.value = new Date();
    inFlight = false;
  }
}

function start(): void {
  if (timer !== null) return;
  void check();
  timer = window.setInterval(() => void check(), 20_000);
}

export function useServerHealth() {
  start();
  return { online, lastChecked, details, check };
}
