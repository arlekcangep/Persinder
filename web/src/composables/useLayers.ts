import { computed, ref } from "vue";

import type { LayerCounts, LayerFilter } from "@/domain/layers";

import { useSearch } from "./useSearch";

const layer = ref<LayerFilter>("both");

export function useLayers() {
  const { state } = useSearch();

  const counts = computed<LayerCounts>(() => ({
    all: state.semantic.length + state.morph.length,
    semantic: state.semantic.length,
    morph: state.morph.length,
  }));

  /** Группы выдачи с учётом выбранного слоя; порядок — семантика, затем морфология. */
  const visible = computed(() => ({
    semantic: layer.value === "morph" ? [] : state.semantic,
    morph: layer.value === "semantic" ? [] : state.morph,
  }));

  const total = computed(() => visible.value.semantic.length + visible.value.morph.length);

  function setLayer(next: LayerFilter): void {
    layer.value = next;
  }

  return { layer, counts, visible, total, setLayer };
}
