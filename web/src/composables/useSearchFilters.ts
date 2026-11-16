import { reactive } from "vue";

export interface SearchFilterValues {
  minScore: number;
  morphLimit: number;
  semanticLimit: number;
}

export const DEFAULT_FILTERS: SearchFilterValues = {
  minScore: 0.8,
  morphLimit: 20,
  semanticLimit: 20,
};

const MAX_LIMIT = 100;

const filters = reactive<SearchFilterValues>({ ...DEFAULT_FILTERS });

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}

function normalize(next: SearchFilterValues): SearchFilterValues {
  return {
    minScore: clamp(next.minScore, 0, 1),
    morphLimit: Math.round(clamp(next.morphLimit, 1, MAX_LIMIT)),
    semanticLimit: Math.round(clamp(next.semanticLimit, 1, MAX_LIMIT)),
  };
}

export function useSearchFilters() {
  function apply(next: SearchFilterValues): void {
    Object.assign(filters, normalize(next));
  }

  function reset(): void {
    Object.assign(filters, DEFAULT_FILTERS);
  }

  return { filters, apply, reset, defaults: DEFAULT_FILTERS };
}
