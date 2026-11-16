import { readonly, ref } from "vue";

export type Theme = "light" | "dark";

const STORAGE_KEY = "persinder-theme";
const theme = ref<Theme>("light");

let initialized = false;
let followSystem = false;
let media: MediaQueryList | null = null;

function systemTheme(): Theme {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function apply(next: Theme): void {
  theme.value = next;
  document.documentElement.dataset.theme = next;
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (meta) meta.setAttribute("content", next === "dark" ? "dark" : "light");
}

function onSystemChange(): void {
  if (followSystem) apply(systemTheme());
}

function init(): void {
  if (initialized) return;
  initialized = true;

  let stored: Theme | null = null;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === "light" || raw === "dark") stored = raw;
  } catch {
    stored = null;
  }

  if (stored) {
    apply(stored);
    return;
  }

  followSystem = true;
  apply(systemTheme());
  media = window.matchMedia("(prefers-color-scheme: dark)");
  media.addEventListener("change", onSystemChange);
}

function toggle(): void {
  followSystem = false;
  apply(theme.value === "dark" ? "light" : "dark");
  try {
    localStorage.setItem(STORAGE_KEY, theme.value);
  } catch {
    /* приватный режим — выбор просто не запомнится */
  }
}

function setTheme(next: Theme): void {
  if (next === theme.value) return;
  toggle();
}

export function useTheme() {
  init();
  return { theme: readonly(theme), toggle, setTheme };
}
