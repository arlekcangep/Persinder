#!/usr/bin/env bash
# run.sh — запуск единого сервиса Persinder.
#
# По умолчанию — быстрый инкрементальный старт по сохранённому журналу:
# файлы, которые уже в базе и с сайдкаром, не трогаются вовсе (watcher
# сравнивает размер/mtime и не выдаёт событий для неизменённых файлов).
# Источники без сайдкара под активной моделью досчитываются сами.
# Полная переиндексация (сброс журнала + перескан всех областей):
#
#   ./run.sh --reindex
#   REINDEX=1 ./run.sh
#
# Обычные флаги пробрасываются в `persinder serve` как есть:
#   ./run.sh --config PATH        — явный конфиг
#   ./run.sh --home PATH          — каталог приложения
#   ./run.sh --host 0.0.0.0 --port 8756
#
# Лимит CPU для эмбеддингов (чтобы индексация не вешала ПК), в процентах:
#
#   PERSINDER_CPU_LIMIT=30 ./run.sh   # не больше 30% всех ядер
#   PERSINDER_EMBED_WORKERS=2 ./run.sh  # параллельных мини-батчей (по умолчанию ~половина ядер)
#
# Вся логика — в `persinder serve` (src/persinder/__main__.py): поиск конфига,
# создание home, watcher, эмбеддинги. Здесь только выбор python из venv и exec.
#
# Требуется: ./setup.sh уже выполнен (venv .venv + модель).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Общий выбор python (см. scripts/py.sh): задаёт PY и PY_FROM_VENV.
# shellcheck source=scripts/py.sh
source "$REPO/scripts/py.sh"
if [ "$PY_FROM_VENV" != "1" ]; then
    echo "ОШИБКА: venv проекта не найден (проверены .venv и PERSINDER_VENV)." >&2
    echo "Сначала запусти ./setup.sh (или scripts/venv.sh)" >&2
    exit 1
fi

# Инкрементальный старт по умолчанию; полный пересчёт: --reindex / REINDEX=1.
reindex="0"
filtered=()
for arg in "$@"; do
    case "$arg" in
        --no-reindex) reindex="0" ;;
        --reindex)    reindex="1" ;;
        *)            filtered+=("$arg") ;;
    esac
done
set -- "${filtered[@]}"
if [ "${REINDEX:-$reindex}" = "1" ]; then
    set -- --reindex "$@"
fi

exec "$PY" -m persinder serve "$@"
