/** Формы JSON-ответов серверного API Persinder. */

export interface ApiHit {
  document_id: string;
  chunk_index: number;
  score: number;
  /** Символьные границы чанка в документе — по ним клиент запрашивает /context. */
  chunk_start: number | null;
  chunk_end: number | null;
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
  /** Адрес, если сервер его сообщает (иначе берётся из location). */
  address?: string;
  documents?: number;
  vectors?: number;
  model?: string;
  pending_embeddings?: number;
  /** Состояние фонового потока эмбеддингов: работает / остановлен / выключено. */
  embedding?: string;
  /** Причина, по которой счётчики не пришли (сервер при этом жив). */
  stats_error?: string;
}

export type ApiLogLevel = "info" | "warning" | "error";

/** Вид события: индексация файлов, эмбеддинги/сайдкары, служебные сообщения. */
export type ApiLogKind = "index" | "embed" | "system";

export interface ApiLogEvent {
  id: number;
  /** ISO-метка времени (UTC). */
  ts: string;
  level: ApiLogLevel;
  kind: ApiLogKind;
  message: string;
}

export interface ApiLogResponse {
  events: ApiLogEvent[];
  /** Последний выданный id — для догрузки новыми запросами ``after``. */
  last_id: number;
}
