import type { ApiHit } from "@/api/types";

/** Строка выдачи: документ, а не отдельный чанк. */
export interface ResultItem {
  key: string;
  documentId: string;
  chunkIndex: number;
  score: number;
  snippet: string;
  area: string;
  areaLabel: string;
  sourcePath: string;
  filename: string;
  title: string;
  dir: string;
  hasPath: boolean;
}

export interface SnippetPart {
  text: string;
  match: boolean;
}

const AREA_LABELS: Record<string, string> = {
  personal: "Личное",
  foreign: "Чужое",
  web: "Web",
};

const MAX_SNIPPET = 300;

export function areaLabel(area: string): string {
  return AREA_LABELS[area] ?? area ?? "Без области";
}

/** Разделить путь на каталог (с разделителем) и имя файла. */
export function splitPath(path: string): { dir: string; file: string } {
  const index = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
  if (index < 0) return { dir: "", file: path };
  return { dir: path.slice(0, index + 1), file: path.slice(index + 1) };
}

function shorten(text: string): string {
  if (text.length <= MAX_SNIPPET) return text;
  const cut = text.slice(0, MAX_SNIPPET);
  const lastSpace = cut.lastIndexOf(" ");
  const body = (lastSpace > MAX_SNIPPET - 60 ? cut.slice(0, lastSpace) : cut).trimEnd();
  return `${body}…`;
}

/** Контекст из API — окно вокруг чанка; убираем markdown-заголовки и схлопываем строки. */
export function makeSnippet(context: string | null): string {
  if (!context) return "";
  const text = context
    .split(/\r?\n/)
    .map((line) => line.replace(/^#{1,6}\s+/, ""))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "";
  const body = shorten(text);
  const tail = /[.!?…»"”]$/.test(body) ? "" : "…";
  return `…${body}${tail}`;
}

export function hitToResult(hit: ApiHit): ResultItem {
  const { dir, file } = splitPath(hit.source_path);
  const filename = hit.filename || file || "Без имени";
  return {
    key: `${hit.document_id}\u0000${hit.source_path}`,
    documentId: hit.document_id,
    chunkIndex: hit.chunk_index,
    score: hit.score,
    snippet: makeSnippet(hit.context),
    area: hit.area,
    areaLabel: areaLabel(hit.area),
    sourcePath: hit.source_path,
    filename,
    title: filename,
    dir,
    hasPath: Boolean(hit.source_path),
  };
}

/**
 * Свернуть чанки в документы: один файл — одна строка выдачи.
 * Порядок сервера сохраняется, берётся лучший score.
 *
 * ``direction`` задаёт, что считать лучшим: косинус у семантики больше —
 * лучше, а сырой bm25 у морфологии меньше — лучше.
 */
export function groupHits(
  hits: ApiHit[],
  direction: "higher" | "lower" = "higher",
): ResultItem[] {
  const byDocument = new Map<string, ResultItem>();
  for (const hit of hits) {
    const item = hitToResult(hit);
    const existing = byDocument.get(item.key);
    if (!existing) {
      byDocument.set(item.key, item);
      continue;
    }
    const better =
      direction === "higher" ? item.score > existing.score : item.score < existing.score;
    const best = better ? item : existing;
    byDocument.set(item.key, {
      ...best,
      snippet: best.snippet || existing.snippet || item.snippet,
    });
  }
  return [...byDocument.values()];
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Разбить сниппет на куски для подсветки слов запроса. */
export function splitHighlight(text: string, query: string): SnippetPart[] {
  const words = [...new Set(query.trim().split(/\s+/).filter((word) => word.length >= 2))];
  if (!words.length) return [{ text, match: false }];

  words.sort((a, b) => b.length - a.length);
  const pattern = new RegExp(`(${words.map(escapeRegExp).join("|")})`, "gi");
  const lowered = new Set(words.map((word) => word.toLowerCase()));

  return text
    .split(pattern)
    .filter(Boolean)
    .map((part) => ({ text: part, match: lowered.has(part.toLowerCase()) }));
}
