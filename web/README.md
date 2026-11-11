# Persinder Web

Веб-клиент Persinder: Vue 3 + TypeScript + Vite. Интерфейс повторяет плоский
эскиз из [`mockups/search`](mockups/search) и ходит в реальный API сервера
(`/search`, `/health`).

## Запуск

```bash
cd web
npm install --no-bin-links   # на vboxfs иначе EPROTO на .bin-симлинках
npm run dev
```

Vite поднимает `http://127.0.0.1:5173` и проксирует `/search`, `/health`,
`/ingest` на `http://127.0.0.1:8756` — туда же, куда смотрит `persinder serve`.
Другой адрес API задаётся переменной `PERSINDER_API`:

```bash
PERSINDER_API=http://127.0.0.1:9000 npm run dev
```

Сервер для разработки:

```bash
persinder serve --config ~/dev-corpus/persinder.yaml --home ~/dev-home
```

## Скрипты

| Команда | Что делает |
|---|---|
| `npm run dev` | dev-сервер Vite с прокси на API |
| `npm run typecheck` | `vue-tsc --noEmit` |
| `npm run build` | проверка типов + production-сборка в `dist/` |
| `npm run preview` | предпросмотр собранной статики |
| `npm test` | unit-тесты (Vitest + jsdom) |

На vboxfs npm-скрипты вызывают `node node_modules/...` напрямую: `.bin`-симлинки
там не создаются.

## Устройство

- `src/api/` — типы ответов и клиент (`search`, `health`); ошибки — `ApiError`.
- `src/domain/` — чистая логика: свёртка чанков в документы (`groupHits`),
  сниппеты, подсветка, области, слои.
- `src/composables/` — singleton-состояние без Pinia: поиск с debounce и
  отменой запросов, фильтры, тема, тосты, здоровье сервера, выбранный слой.
- `src/components/` — шапка, вкладки слоёв, параметры, строка выдачи, статус
  сервера со статистикой, тост.
- `src/views/` — `SearchView` (выдача и состояния) и `SettingsView`.
- `src/styles/app.css` — палитра эскиза, тёмная тема и адаптив.

Запрос хранится в URL (`/?q=…`), поэтому ссылку можно переслать, а перезагрузка
возвращает ту же выдачу. Тема запоминается в `localStorage`; по умолчанию
берётся системная.

## Что пока заглушки

- `/health` отдаёт только `{"status": "ok"}` — документы, векторы и модель в
  статистике показываются как «—». Когда появится эндпоинт статистики, поля
  `documents`, `vectors`, `model`, `pending_embeddings` подхватятся сами.
- «Открыть» и «Показать в папке» показывают тост: действия должен выполнять
  сервер, эндпоинтов для них ещё нет.
- Папки и модели в настройках не редактируются: сервер не отдаёт конфигурацию.
  В «Общих» работает переключение темы.

## Проверка

```bash
npm run typecheck && npm test && npm run build
```

E2E-проверки вручную через Playwright Chromium (в контейнере):

```bash
playwright screenshot --viewport-size "390,844" http://127.0.0.1:5173/ mobile.png
```
