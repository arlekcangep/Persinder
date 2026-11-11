import type {
  ApiHealthResponse,
  ApiLogKind,
  ApiLogResponse,
  ApiSearchResponse,
} from "./types";

export interface SearchParams {
  text: string;
  minScore: number;
  morphLimit: number;
  semanticLimit: number;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** В разработке пусто — запросы идут через прокси Vite на 127.0.0.1:8756. */
const base = (import.meta.env.VITE_API_BASE ?? "").replace(/\/+$/, "");

function apiUrl(path: string): string {
  return `${base}${path}`;
}

async function readError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { error?: unknown };
    if (typeof body.error === "string" && body.error) return body.error;
  } catch {
    /* тело не JSON — покажем статус */
  }
  return `сервер вернул ${response.status}`;
}

export async function search(
  params: SearchParams,
  signal?: AbortSignal,
): Promise<ApiSearchResponse> {
  const query = new URLSearchParams({
    q: params.text,
    limit: String(Math.max(params.morphLimit, params.semanticLimit)),
    min_score: String(params.minScore),
    morph_limit: String(params.morphLimit),
    semantic_limit: String(params.semanticLimit),
  });

  const response = await fetch(apiUrl(`/search?${query.toString()}`), {
    signal,
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }
  return (await response.json()) as ApiSearchResponse;
}

export async function health(signal?: AbortSignal): Promise<ApiHealthResponse> {
  const response = await fetch(apiUrl("/health"), {
    signal,
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }
  return (await response.json()) as ApiHealthResponse;
}

export interface LogsParams {
  /** Сколько последних событий вернуть. */
  limit?: number;
  /** Только события с id больше указанного. */
  after?: number;
  /** Только события этого вида. */
  kind?: ApiLogKind;
  /** Нижняя граница времени (ISO-дата-время, включительно). */
  from?: string;
  /** Верхняя граница времени (ISO-дата-время, исключительно). */
  to?: string;
}

export async function fetchLogs(
  params: LogsParams = {},
  signal?: AbortSignal,
): Promise<ApiLogResponse> {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.after !== undefined) query.set("after", String(params.after));
  if (params.kind) query.set("kind", params.kind);
  if (params.from) query.set("from", params.from);
  if (params.to) query.set("to", params.to);
  const qs = query.toString();
  const response = await fetch(apiUrl(`/logs${qs ? `?${qs}` : ""}`), {
    signal,
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }
  return (await response.json()) as ApiLogResponse;
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}
