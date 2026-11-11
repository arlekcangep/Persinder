# Persinder Web

Веб-клиент Persinder: Vue 3 + TypeScript + Vite. Интерфейс повторяет плоский
эскиз из [`mockups/search`](mockups/search) и ходит в реальный API сервера
(`/search`, `/health`).

## Для пользователя: собирать ничего не нужно

Собранный клиент уже лежит в Python-пакете —
`src/persinder/api/static/` — и раздаётся тем же сервером, что и API:

```bash
./run.sh
# или
persinder serve --config persinder.yaml
```

Откройте в браузере адрес, который печатает сервер:

```text
http://127.0.0.1:8756/
```

Node.js, npm и сборка на машине пользователя не требуются. `/`, `/assets/…` и
маршруты вида `/settings` отдаёт Python; статика едет внутри пакета (см.
`[tool.setuptools.package-data]` в `pyproject.toml`).

## Разработка

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
| `npm run build` | проверка типов + сборка в `dist/` |
| `npm run build:static` | сборка + копирование в `src/persinder/api/static/` |
| `npm run preview` | предпросмотр собранной статики |
| `npm test` | unit-тесты (Vitest + jsdom) |

На vboxfs npm-скрипты вызывают `node node_modules/...` напрямую: `.bin`-симлинки
там не создаются.

## Как обновить встроенный клиент

`src/persinder/api/static/` — не ручная копия, а артефакт сборки. После правок
в `web/src`:

```bash
cd web
npm run build:static
```

Команда проверит типы, соберёт Vite и заменит `src/persinder/api/static/`
целиком (старые хешированные файлы удаляются). Изменения в `web/` и
`src/persinder/api/static/` коммитятся вместе — тогда у всех, кто ставит
Persinder, сразу есть актуальный интерфейс.

Проверка после сборки:

```bash
cd ..
pytest tests/test_api.py -q
./run.sh   # открыть http://127.0.0.1:8756/
```

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
- `scripts/copy-static.mjs` — копирование `dist/` в Python-пакет.

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
npm run typecheck && npm test && npm run build:static
```

E2E-проверки вручную через Playwright Chromium (в контейнере):

```bash
playwright screenshot --viewport-size "390,844" http://127.0.0.1:8756/ mobile.png
```
