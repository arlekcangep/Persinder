// Копирует собранный Vite клиент в Python-пакет, чтобы `persinder serve`
// раздавал его без Node.js на машине пользователя.
import { cp, mkdir, rm, stat } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(here, "..");
const dist = resolve(webRoot, "dist");
const target = resolve(webRoot, "..", "src", "persinder", "api", "static");

try {
  await stat(resolve(dist, "index.html"));
} catch {
  console.error("Нет web/dist/index.html — сначала выполните npm run build.");
  process.exit(1);
}

await rm(target, { recursive: true, force: true });
await mkdir(target, { recursive: true });
await cp(dist, target, { recursive: true });

console.log(`Статика скопирована: ${target}`);
