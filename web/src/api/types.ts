/** Формы JSON-ответов серверного API Persinder. */

export interface ApiHit {
  document_id: string;
  chunk_index: number;
  score: number;
  context: string | null;
  area: string;
  source_path: string;
  filename: string;
}

export interface ApiSearchResponse {
  query: string;
  /** Морфологический слой (FTS5). */
  hits: ApiHit[];
  /** Семантический слой (векторы). */
  semantic_hits: ApiHit[];
}

export interface ApiHealthResponse {
  status: string;
  /** Появятся, когда у сервера будет эндпоинт статистики. */
  address?: string;
  documents?: number;
  vectors?: number;
  model?: string;
  pending_embeddings?: number;
}
