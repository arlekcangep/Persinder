import { computed, reactive } from "vue";

import { ApiError, isAbortError, search } from "@/api/client";
import { groupHits, type ResultItem } from "@/domain/results";

import { useSearchFilters } from "./useSearchFilters";

interface SearchState {
  /** Текст в поле ввода. */
  query: string;
  /** Запрос, по которому сейчас показана выдача. */
  activeQuery: string;
  semantic: ResultItem[];
  morph: ResultItem[];
  loading: boolean;
  error: string | null;
  searched: boolean;
}

const DEBOUNCE_MS = 250;

const state = reactive<SearchState>({
  query: "",
  activeQuery: "",
  semantic: [],
  morph: [],
  loading: false,
  error: null,
  searched: false,
});

let requestId = 0;
let controller: AbortController | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;

const totalCount = computed(() => state.semantic.length + state.morph.length);

function clearTimer(): void {
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
}

function abortRequest(): void {
  controller?.abort();
  controller = null;
}

function resetResults(): void {
  state.semantic = [];
  state.morph = [];
  state.searched = false;
}

async function runNow(): Promise<void> {
  clearTimer();
  abortRequest();

  const text = state.query.trim();
  if (!text) {
    requestId += 1;
    state.activeQuery = "";
    state.loading = false;
    state.error = null;
    resetResults();
    return;
  }

  const id = ++requestId;
  const { filters } = useSearchFilters();
  state.activeQuery = text;
  state.loading = true;
  state.error = null;
  controller = new AbortController();

  try {
    const response = await search(
      {
        text,
        minScore: filters.minScore,
        morphLimit: filters.morphLimit,
        semanticLimit: filters.semanticLimit,
      },
      controller.signal,
    );
    if (id !== requestId) return;
    state.semantic = groupHits(response.semantic_hits);
    state.morph = groupHits(response.hits, "lower");
    state.searched = true;
  } catch (error) {
    if (id !== requestId || isAbortError(error)) return;
    state.semantic = [];
    state.morph = [];
    state.searched = true;
    state.error =
      error instanceof ApiError
        ? error.message
        : "Проверьте, что запущен persinder serve, и повторите запрос.";
  } finally {
    if (id === requestId) {
      state.loading = false;
      controller = null;
    }
  }
}

function schedule(delay = DEBOUNCE_MS): void {
  clearTimer();
  timer = setTimeout(() => {
    timer = null;
    void runNow();
  }, delay);
}

function setQuery(value: string): void {
  state.query = value;
  state.error = null;

  if (!value.trim()) {
    clearTimer();
    abortRequest();
    requestId += 1;
    state.activeQuery = "";
    state.loading = false;
    resetResults();
    return;
  }

  schedule();
}

function clear(): void {
  setQuery("");
}

function useSearch() {
  return {
    state,
    totalCount,
    runNow,
    setQuery,
    clear,
  };
}

export { useSearch };
