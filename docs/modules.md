# Модули Persinder

> Декомпозиция на модули и их интерфейсы. Служит картой кода: что за что отвечает
> и где границы между частями.

## Принципы

- Модуль = одна ответственность и узкий интерфейс.
- Ядро — на стандартной библиотеке Python; внешние библиотеки подключаются за
  интерфейсом (адаптеры). Часть из них — обязательные зависимости пакета
  (onnxruntime — эмбеддинги работают «из коробки»), часть — опциональные extras
  (watch, pdf, http).
- Модули общаются через интерфейсы (Protocol), а не через конкретные реализации.
- **Clean Architecture (ADR D18, §3.1 архитектуры):** зависимости направлены внутрь.
  Модели разделяются по слоям — доменные, БД (persistence), HTTP (DTO) и внутренние
  модели сервисов. Общий `models.py` на весь проект не используется.

## Карта модулей

| Модуль | Ответственность | Статус |
|---|---|---|
| `config` | настройки, каталоги, области | реализован |
| `domain` | ядро: доменные модели (Area, Document, Chunk, EmbeddingJob) | реализован |
| `interfaces` | протоколы/порты (Stemmer, TextStore, MetaStore…) | реализован |
| `morphology` | нормализация, токенизация, стемминг | реализован |
| `textstore` | временное хранение сырого текста | реализован |
| `store` | пакет: `models` (модели БД) + `sqlite` (SQLite: документы, FTS5, очередь) | реализован |
| `ingest` | пакет: `models` (вход/выход) + `service` (морфоиндекс + очередь + удаление) + `journal` (журнал вызовов) | реализован |
| `api` | пакет: `models` (DTO) + `http` (HTTP-обёртка над `ingest`) | реализован |
| `embed` | пакет: `models` + `service` (воркер: очередь → векторы) + `sidecar` (сайдкары на модель) + `registry` (каталог/установка моделей) + `state` (embedding/search, D24) | реализован |
| `vectorindex` | пакет: `service` (VectorSearch: ANN USearch + фолбэк-скан) + `index` (обёртка USearch) | реализован (USearch) |
| `sources` | watcher'ы, журнал статусов и экстракторы областей | реализован |
| `search` | фасад запроса (три группы, порядок) | реализован |

Заглушки — это модули с зафиксированным интерфейсом и пустой/минимальной логикой,
чтобы очертить границы.

## Модели по слоям

По ADR D18 (см. §3.1 архитектуры) каждый слой владеет своими моделями. Раскладка:

```
src/persinder/
  domain/
    models.py        # Area, Document, Chunk, EmbeddingJob — ядро, без зависимостей
    chunking.py      # chunk_text — нарезка текста на чанки (общая для ingest и embed)
    clock.py         # utcnow()
  ingest/
    models.py        # IngestRequest, IngestResult, RemoveResult, RecoveryStats
    service.py       # IngestService
    journal.py       # журнал вызовов ingest (JSONL) + восстановление (D23)
  embed/
    models.py        # EmbedFingerprint, ChunkParams, EmbedStats
    service.py       # EmbedWorker (очередь → векторы → сайдкары, по моделям) + PlaceholderEmbedder
    onnx.py          # OnnxEmbedder — реальная модель e5-small (ONNX INT8)
    sidecar.py       # сайдкары: чтение/запись (бинарный формат, атомарность), удаление по моделям
    registry.py      # каталог моделей, install/remove весов, build_embedder (D24)
    state.py         # ModelState: embedding/search (data_dir/models.json), queue_models (D24)
  store/
    models.py        # MorphHit, EmbeddingQueueItem, StoreCounts — модели БД
    sqlite.py        # SqliteStore
  api/
    models.py        # IngestPayload, IngestResponse, PayloadError — DTO транспорта
    http.py          # IngestHTTPServer, make_server, serve
  sources/
    models.py        # AreaConfig, WatchConfig, AreaScan, ChangeEvent, ScanResult
    config.py        # загрузка YAML-конфига областей и watcher'а
    scanner.py       # polling-скан (snapshot + diff + debounce)
    watcher.py       # цикл скана, запуск по требованию
    journal.py       # журнал статусов файлов (JSONL) — состояние watcher'а (D21),
                     # обрезка истории переходов
    extract.py       # экстракторы (markdown/text/html/mht/pdf), контейнер
                     # archive (zip/7z/tar/gz/rar), фабрика и реестр
                     # (ExtractorFactory, ContainerExtractor)
    pipeline.py      # события → экстракция (файл или части контейнера) →
                     # ingest (документ на каждую часть) → журнал
```

Переход между слоями — только через явные мапперы (`IngestPayload.to_request`,
`IngestResponse.from_result`). Порты (`interfaces.py`) импортируют модели слоёв
только для аннотаций (`TYPE_CHECKING`), чтобы не создавать циклических зависимостей.

## Поток данных

```
источник (Syncthing)                     ┌── ветка быстрого ответа ──┐
      │                                  │  (морфологический слой)  │
      ▼                                  ▼                          │
 [sources] ──текст+метаданные──▶ [ingest] ──▶ [store] (FTS5) ───────┘
      │                                  │
      │                                  ├──▶ [textstore] (временный каталог)
      │                                  └──▶ [store].embedding_queue
      │                                                │
      │                                                ▼
      └──────────────────────────────▶ [embed] ──▶ [vectorindex] + сайдкары
```

## Модуль 1: `ingest` (первый реализуемый)

**Назначение.** Принимает уже извлечённый текст и метаданные, делает первый (быстрый)
морфологический слой индексации, кладёт текст во временный каталог и ставит задачу на
фоновую индексацию по эмбеддингам.

**Вход:**

```json
{ "filename": "DeepLearning.pdf", "area": "foreign", "text": "...", "title": null }
```

- `area` — тип папки: `personal` | `foreign` | `web`.

**Выход:**

```json
{ "status": "indexed", "document_id": "…", "chunks": 12 }
```

`status`: `indexed` | `already_indexed` (дубликат по хэшу) | `rejected` (пустой текст
и т. п.).

**Что делает внутри (по шагам):**

1. Проверяет вход и нормализует `area`.
2. Считает хэш содержимого — он же `document_id` (дедупликация, решение D12).
3. Дубликат → добавляет источник и отвечает `already_indexed`, не индексируя заново.
4. Кладёт сырой текст во **временный каталог** (`textstore`).
5. Режет текст на чанки (размер и перекрытие — из настроек).
6. Для каждого чанка считает стеммированный текст и **синхронно** пишет в FTS5
   (морфологический слой).
7. Атомарно пишет документ, источники, чанки и **задачи на эмбеддинги** в очередь SQLite.
8. Возвращает результат.

**Чего модуль НЕ делает:** не считает эмбеддинги, не строит векторный индекс, не
извлекает текст из файлов. Это задачи `embed`, `vectorindex` и `sources`.

### Интерфейсы модуля

```python
class IngestService:
    def ingest(self, request: IngestRequest) -> IngestResult: ...
    def remove_source(self, area: Area, filename: str) -> RemoveResult: ...
    def recover(self) -> RecoveryStats: ...


class Stemmer(Protocol):
    name: str

    def stem(self, tokens: Iterable[str]) -> list[str]: ...


class TextStore(Protocol):
    def put(self, key: str, text: str) -> str: ...
    def read(self, key: str) -> str: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class MetaStore(Protocol):
    def find_document_by_hash(self, content_hash: str) -> str | None: ...
    def add_source(self, doc_id, area, filename, source_path="") -> list[str]: ...
    def source_document(self, area: str, filename: str) -> str | None: ...
    def source_path(self, area: str, filename: str) -> str: ...
    def remove_source(self, area: str, filename: str) -> str | None: ...
    def save_document(self, doc, source, chunks, jobs, source_path="") -> list[str]: ...
    def enqueue_document(self, doc_id, area, source_path="") -> None: ...
    def pending_embeddings(self, limit: int = 100) -> list[EmbeddingQueueItem]: ...
    def pending_for_docs(self, doc_ids: list[str]) -> list[EmbeddingQueueItem]: ...
    def drop_queue_rows(self, ids: list[int]) -> None: ...
    def search_morph(self, stemmed_query: str, limit: int) -> list[dict]: ...
    def chunk_span(self, doc_id: str, chunk_index: int) -> tuple[int, int] | None: ...


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...
```

Сервис `ingest` — единственная точка доступа к БД и хранилищу текста: правка и
удаление возвращают вытесненные документы, и их сырой текст удаляется **тут же**
(D22). Брошенное крахом подчищает `recover()` по журналу вызовов (D23). Сайдкар —
тоже производное документа: при вытеснении/удалении `ingest` убирает его рядом
с текстом (D24).

## Модуль 2: `embed`

**Назначение.** Разбирает очередь эмбеддингов: берёт документ, читает его сырой
текст, нарезает теми же чанками (общий `domain.chunk_text`), считает векторы через
порт `Embedder` и пишет **сайдкар** (§7) атомарно. Запускается по требованию
(`persinder embed`), ноль ресурсов в простое.

- Реальный файл → сайдкар `.<каталог>/.persinder/<модель>/<файл>.pdr` (путь
  источника идёт через `IngestRequest.source_path`; подпапка на модель — у файла
  живут эмбеды нескольких моделей одновременно, D24).
- Виртуальный элемент (нет файла на диске — web, вставленный текст) → сайдкар в
  отдельной папке `Settings.virtual_dir` (`data_dir/virtual/<модель>/<doc_id>.pdr`).
- **Модель-менеджмент (D24):** реестр `embed/registry.py` (каталог `CATALOG`,
  установка/удаление весов, фабрика `build_embedder`), состояние на профиль
  `embed/state.py` (`data_dir/models.json`): `embedding` (чем считаем новые файлы)
  и `search` (чем ищем). CLI: `persinder model list/install/use/remove`,
  `persinder embed --all/--background`. Миграция стартует при `install`
  (`embedding` переключается, все источники ставятся на пересчёт), поиск
  переключается отдельным `use`; пока хендлы разные, новые файлы индексируются
  обеими моделями.
- Эмбеддер — детерминированная заглушка `PlaceholderEmbedder` (`placeholder-v1`,
  встроена) или реальная модель `OnnxEmbedder` (e5-small INT8, §12.1) из
  `<home>/models/<ключ>/` (корень — `Settings.models_dir`). Эмбеддер
  подбирается **фабрикой по ключу задачи** очереди — один проход уживает задачи
  разных моделей.
- Удаление модели (`model remove`) убирает веса и **все её сайдкары в один момент**
  (не раньше); удаление/вытеснение источника чистит его сайдкары по всем моделям
  в `ingest` (владелец инварианта «документ + текст + сайдкары»).

## Примечание о HTTP-сервисе и ресурсах

HTTP-сервис слушает только локальный интерфейс и запускается по требованию приложением;
это не постоянно работающий демон. Требование «ноль ресурсов в простое» сохраняется:
когда сервис не запущен, он ничего не потребляет.

## Модуль 3: `vectorindex`

**Назначение.** Семантический поиск: запрос → вектор (`embed_query`, префикс
`query:`, §12.1) активной поисковой модели → ANN (USearch) → top-k. Ноль ресурсов
в простое: индекс — mmap-файл, резидентной памяти не занимает; если файла нет,
поиск падает на точный скан таблицы.

- **Хранение.** Векторы чанков материализует воркер `embed` в таблицу
  `embeddings` `(id, model, doc_id, chunk_index, vector f16, UNIQUE(...))` —
  в той же операции, где пишется сайдкар (§8). Таблица — локальное производное:
  удаляется каскадом по документу (FK ON DELETE CASCADE) и целиком по модели
  (`model remove` → `drop_embeddings_model`). `id` — стабильный ключ строки,
  upsert вектора его сохраняет.
- **ANN-индекс.** `UsannIndex` (`index.py`) — обёртка USearch: один mmap-файл на
  модель `data_dir/ann/<модель>.usearch`, метрика косинус, ключи — `id` строк
  `embeddings`. Воркер добавляет векторы сразу при расчёте (дубликат ключа
  пропускается) и реконсилирует протухшие в конце прохода; `rebuild_index`
  (`persinder ann rebuild`) пересобирает из таблицы. ⚠️ Импорт USearch —
  строго **после** onnxruntime (иначе segfault), поэтому в `index.py` он ленивый
  через `_index_class()`.
- **Поиск.** `VectorSearch(store, build_embedder, state.search, ann_dir)`:
  если файл индекса есть — top-k из USearch, ключи сопоставляются с таблицей
  (протухшие отсекаются); иначе — полный скан `embeddings_for_model` с косинусом
  (векторы нормированы, косинус — скалярное произведение).
- **Форма результата.** Семантические хиты — отдельное поле `semantic_hits`
  рядом с морфологическими `hits` (модуль 1): группы видны и сравнимы,
  API/CLI не ломаются. Объединение в единый рейтинг (D16) — следующий шаг.
  `SearchService` обогащает каждый хит источником (`area`, `source_path`,
  `filename` из `document_sources`; у дубликата берётся реальный файл, а не
  виртуальная запись) и, если есть `TextStore`, полем `context` — текст
  чанка плюс три строки до и три после (`search/context.py`).
- **CLI/API.** `persinder search <text> [--limit N] [--home …] [--host …]
  [--port …]` — HTTP-first: если запущенный `serve` отвечает, CLI делает
  `GET /search` и видит те же модели и скоупы, что сервер; локальный поиск —
  fallback с предупреждением, а `--local` (или явный `--config`) включает его
  принудительно. `persinder ann rebuild [--model KEY]`; `GET /search` отвечает
  `{query, hits, semantic_hits}`, у каждого хита есть
  `{document_id, chunk_index, score, context, area, source_path, filename}`.
- **Дальше.** При росте базы — шардирование индекса по коллекциям и компакция
  (§8); формат векторов (f16-блоб) и интерфейс `VectorSearch` зафиксированы.
