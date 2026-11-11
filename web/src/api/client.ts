import type { ApiHealthResponse, ApiSearchResponse } from "./types";

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

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}
