import { afterEach, describe, expect, it } from "vitest";

import { DEFAULT_FILTERS, useSearchFilters } from "@/composables/useSearchFilters";

const filters = useSearchFilters();

afterEach(() => filters.reset());

describe("useSearchFilters", () => {
  it("начинает со значений по умолчанию", () => {
    expect(filters.filters).toEqual(DEFAULT_FILTERS);
  });

  it("применяет нормализованные значения", () => {
    filters.apply({ minScore: 0.9, morphLimit: 7, semanticLimit: 12 });

    expect(filters.filters.minScore).toBe(0.9);
    expect(filters.filters.morphLimit).toBe(7);
    expect(filters.filters.semanticLimit).toBe(12);
  });

  it("ограничивает min_score отрезком [0, 1]", () => {
    filters.apply({ minScore: 1.7, morphLimit: 20, semanticLimit: 20 });
    expect(filters.filters.minScore).toBe(1);

    filters.apply({ minScore: -0.4, morphLimit: 20, semanticLimit: 20 });
    expect(filters.filters.minScore).toBe(0);
  });

  it("держит лимиты в разумных границах и округляет", () => {
    filters.apply({ minScore: 0.8, morphLimit: 0, semanticLimit: 500 });

    expect(filters.filters.morphLimit).toBe(1);
    expect(filters.filters.semanticLimit).toBe(100);
  });

  it("заменяет NaN значением по умолчанию", () => {
    filters.apply({ minScore: Number.NaN, morphLimit: Number.NaN, semanticLimit: Number.NaN });

    expect(filters.filters.minScore).toBe(0);
    expect(filters.filters.morphLimit).toBe(1);
    expect(filters.filters.semanticLimit).toBe(1);
  });

  it("сбрасывает значения", () => {
    filters.apply({ minScore: 0.2, morphLimit: 3, semanticLimit: 4 });
    filters.reset();

    expect(filters.filters).toEqual(DEFAULT_FILTERS);
  });
});
