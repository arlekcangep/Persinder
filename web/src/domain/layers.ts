export type LayerFilter = "both" | "semantic" | "morph";

export type ResultLayer = "semantic" | "morph";

export interface LayerCounts {
  all: number;
  semantic: number;
  morph: number;
}
