# Задачи

Список согласованных задач и статусов. Обновляется по мере выполнения.
Источники: решения этой сессии + разбросанные по `docs/*.md` и `README.md`.

## Текущая итерация: транспорт поиска (FastAPI + asyncio)

### Шаг 1. FastAPI-сервер (замена `ThreadingHTTPServer`)

- [x] `fastapi_server.py`: `build_fastapi_app`, маршруты `/health`, `/search`,
      `/context`, `/context/batch`, `/logs`, `/ingest`, статика.
- [x] `SyncSearchService` — синхронный фасад над портом `Search`, вызов
      оборачивается в `asyncio.to_thread`.
- [x] `serve_fastapi` / `make_fastapi_server` / `PersinderFastAPIServer`
      (убран несуществующий в uvicorn 0.54 `threading_model` — старт проверен
      интеграционным тестом на реальном сервере).
- [x] Тесты (`test_api.py`, `test_main_serve.py`, `test_search_*`) — зелёные
      (запуск через венв `~/venvs/persinder`, не системный python).
- [x] Переключить `cli/serve.py`, `cli/parser.py` с `http.py` на `serve_fastapi`
      (передаются `store`/`textstore`/`registry`; HTTP-only путь в `parser.py`
      тоже переключён).

### Шаг 2. `/context` (вариант C: endpoint сам ходит в БД)

- [x] Класс-резолвер текста (`TextResolver` в `api/textresolver.py`):
      - сначала `FsTextStore` (временное хранилище) — если текст есть, оттуда;
      - если нет — `SELECT source_path, content_hash, extractor`
        из `document_sources JOIN documents ON id = doc_id`;
      - `source_path` непустой → `ExtractorRegistry.extractor_for(extractor)`
        → `extract(path)` → `Extracted.text`;
      - если ничего нет → ошибка (`source_missing`), которую покажет
        web UI / консоль.
- [x] **Резолвер сам режет текст по символам** (`start`, `end`, `offset`;
      вариант б: раз текст у нас есть — клиент оперирует символами, а не
      байтами) — не тащить весь файл наверх; длинные срезы чанкуются по
      байтовому бюджету, но границы всегда на символах.
- [x] Виртуальные документы (`source_path == ''`): пока отдаём ошибку
      (текст виртуалов в FsTextStore; если его нет — перечитывать нечего).
- [x] `build_fastapi_app` получает `store: MetaStore`, `textstore: TextStore`,
      `registry: ExtractorRegistry | None`, `virtual_dir: Path | None`.
- [x] NDJSON: фрагменты по `CONTEXT_CHUNK_BYTES` (1 МБ), статусы
      `source_missing`/`error`.
- [x] Метод `MetaStore.document_source(doc_id)` — документ + главный источник
      (extractor, content_hash, source_path) для варианта C.
- [x] Тесты: `tests/test_context_api.py` (резолвер, потоки NDJSON,
      интеграция через реальный uvicorn: textstore → перечитывание → ошибки).
      Заодно убран сломанный `threading_model=True` из конфига uvicorn
      (в uvicorn 0.54 такого параметра нет).

### Шаг 3. `/search` без контекста

- [x] Убрать `context` из `SearchResponse._hits_to_dict`; в хит добавлены
      `chunk_start`/`chunk_end` — символьные границы чанка из `chunks`
      (иначе клиенту не собрать запрос `/context`).
- [x] `SearchHit.context` убран из датакласса полностью — никто не читал
      (в сети `context` тоже не отдаётся).
- [x] `_enrich` переписан: только источник + спаны; мёртвые параметры
      конструктора `textstore`/`context_lines`/`context_sentences` убраны
      (call sites почищены).
- [x] CLI: после `/search` — обычный `/context` по каждому хиту (без batch),
      сниппеты печатаются по мере прихода; локальный режим — `TextResolver`
      в процессе (`_print_contexts_http` / `_print_contexts_local`).
- [x] Веб-клиент: результаты `/search` показываются сразу, карточки
      наполняются `/context` постепенно (`fetchContext` NDJSON в
      `api/client.ts`, `fillContexts` в `useSearch.ts`, плейсхолдер
      «Контекст загружается…»); бандл в `static/assets` пересобран.
- [x] Тесты: `/search` без `context` (спаны вместо контекста в `test_api.py`),
      CLI-сниппеты (`CliContextTest`), спаны в `test_search_source.py`,
      веб-тесты `web/tests/results.test.ts` обновлены.

## Следующие шаги (после fast_api + /context + /search)

- [ ] **Сжатие zip текста в FsTextStore** — хранить текст сжатым, а не
      разжатым UTF-8.
- [ ] **Потоковый интерфейс экстракторов** — если формат файла поддерживает,
      извлекать только нужный байтовый диапазон, не распаковывая весь файл
      в память.

## Отложено (на этой итерации НЕ делаем)

- [ ] Кэш контекста (LRU) — запрещено на текущей итерации, только константы.
- [ ] Багфикс перекодировки в запрошенную кодировку — отложен, отдавать
      «как было».

## Из md-файлов (docs/modules.md, docs/architecture.md, docs/performance.md)

### Ранжирование и семантика

- [ ] **Единое ранжирование (D16)** — объединить `hits` + `semantic_hits`
      в единый рейтинг (README.md:122, architecture.md:488, modules.md:275).
- [ ] **Лемматизация для морфологии** — Snowball не связывает дериваты
      (`медведь → медвед`, но `медвежонок` не находится по «медведь»).
      Варианты: префиксный поиск по стемам (`медвед*` в FTS5) или
      лемматизация (pymorphy3). Оценить recall, размер словаря, время.
- [ ] **Cross-encoder (реранкер)** — поверх top-K `semantic_hits` прогнать
      «запрос + чанк» мультиязычным cross-encoder (ONNX), переранжировать и
      отсечь по его score. Оценить качество, задержку на CPU, калибровку
      порога.

### Производительность (docs/performance.md)

- [ ] **Прогрев эмбеддера при старте** — загружать модель не на первом
      запросе, а при запуске `serve` (убрать разовый всплеск ~1.5 с).
- [ ] **Память точного скана** — матрица `N × dim` float32 (~45 МБ на 30k
      чанков); для сотен тысяч чанков обрабатывать блоками.

### Индекс и поиск

- [ ] **Шардирование индекса по коллекциям и компакция (§8)** — при росте
      базы; тот же USearch, mmap-файлы, `EXACT_SEARCH_MAX` 100 000.
- [ ] **Китайский язык** — требуется сегментация (CJK): `jieba` на десктопе,
      ICU на Android (architecture.md:595).

### Архитектура (из разделов «на будущее»)

- [ ] **Метаданные элементов sidecar-файлами CSL-JSON (§7.1)** — для
      не-заметок (тэги, коллекции, прогресс); слияние конфликтов
      `*.sync-conflict-*`.
- [ ] **Per-device ignore patterns** — телефон игнорирует сайдкары областей
      `foreign`/`web`, чтобы не тянуть тяжёлый локальный слой.

### Отдельный проект (не Persinder)

- [ ] **Веб и закладки** — скрейпинг веба отдельным проектом; Persinder просто
      подхватывает контент из папки роли `web`.
