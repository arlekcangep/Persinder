import { describe, expect, it } from "vitest";

import type { ApiHit } from "@/api/types";
import {
  areaLabel,
  groupHits,
  makeSnippet,
  splitHighlight,
  splitPath,
} from "@/domain/results";

function hit(overrides: Partial<ApiHit> = {}): ApiHit {
  return {
    document_id: "doc-1",
    chunk_index: 0,
    score: 0.8,
    context: "контекст вокруг чанка",
    area: "personal",
    source_path: "D:\\Obsidian\\заметки.md",
    filename: "заметки.md",
    ...overrides,
  };
}

describe("groupHits", () => {
  it("сворачивает чанки одного документа в одну строку и берёт лучший score", () => {
    const result = groupHits([
      hit({ chunk_index: 0, score: 0.81, context: "первый чанк" }),
      hit({ chunk_index: 3, score: 0.9, context: "лучший чанк" }),
      hit({ document_id: "doc-2", score: 0.7, filename: "другой.md" }),
    ]);

    expect(result).toHaveLength(2);
    expect(result[0].documentId).toBe("doc-1");
    expect(result[0].chunkIndex).toBe(3);
    expect(result[0].snippet).toContain("лучший чанк");
    expect(result[1].documentId).toBe("doc-2");
  });

  it("для bm25 лучшим считается меньшее значение", () => {
    const result = groupHits(
      [
        hit({ chunk_index: 0, score: -1.2, context: "слабее" }),
        hit({ chunk_index: 1, score: -4.5, context: "сильнее" }),
      ],
      "lower",
    );

    expect(result).toHaveLength(1);
    expect(result[0].chunkIndex).toBe(1);
    expect(result[0].snippet).toContain("сильнее");
  });

  it("не сливает документы с одинаковым id, но разными путями", () => {
    const result = groupHits([
      hit({ source_path: "D:\\a\\файл.md" }),
      hit({ source_path: "D:\\b\\файл.md" }),
    ]);

    expect(result).toHaveLength(2);
  });
});

describe("splitHighlight", () => {
  it("подсвечивает слова запроса без учёта регистра", () => {
    const parts = splitHighlight("Серый волчок и Волчок", "волчок");

    expect(parts.filter((part) => part.match).map((part) => part.text)).toEqual([
      "волчок",
      "Волчок",
    ]);
  });

  it("берёт слова от двух букв и экранирует спецсимволы", () => {
    expect(splitHighlight("a+b", "a+b")).toEqual([{ text: "a+b", match: true }]);
    expect(splitHighlight("я и ты", "я")).toEqual([{ text: "я и ты", match: false }]);
  });

  it("возвращает цельный текст для пустого запроса", () => {
    expect(splitHighlight("текст", "  ")).toEqual([{ text: "текст", match: false }]);
  });
});

describe("makeSnippet", () => {
  it("убирает markdown-заголовки и переводы строк", () => {
    expect(makeSnippet("# Заголовок\n\nТекст заметки.")).toBe("…Заголовок Текст заметки.");
  });

  it("обрезает длинный контекст и ставит многоточие", () => {
    const long = `${"слово ".repeat(80)}конец`;
    const snippet = makeSnippet(long);

    expect(snippet.startsWith("…")).toBe(true);
    expect(snippet.endsWith("…")).toBe(true);
    expect(snippet.length).toBeLessThan(320);
  });

  it("не добавляет лишнее многоточие после точки", () => {
    expect(makeSnippet("Короткий текст.")).toBe("…Короткий текст.");
  });

  it("возвращает пустую строку без контекста", () => {
    expect(makeSnippet(null)).toBe("");
    expect(makeSnippet("   ")).toBe("");
  });
});

describe("пути и области", () => {
  it("делит путь на каталог и имя файла", () => {
    expect(splitPath("D:\\Library\\Books\\книга.pdf")).toEqual({
      dir: "D:\\Library\\Books\\",
      file: "книга.pdf",
    });
    expect(splitPath("/home/user/файл.md")).toEqual({
      dir: "/home/user/",
      file: "файл.md",
    });
    expect(splitPath("файл.md")).toEqual({ dir: "", file: "файл.md" });
  });

  it("переводит область на русский", () => {
    expect(areaLabel("personal")).toBe("Личное");
    expect(areaLabel("foreign")).toBe("Чужое");
    expect(areaLabel("web")).toBe("Web");
    expect(areaLabel("custom")).toBe("custom");
  });
});
