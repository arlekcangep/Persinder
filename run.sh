#!/usr/bin/env bash
# run.sh — запуск единого сервиса Persinder.
#
# По умолчанию — полная переиндексация (`--reindex`), как и должно быть:
# при старте читаются полные каталоги всех областей и сверяются с журналом
# по именам (новые индексируются, удалённые убираются), затем в фоне
# сверяются хэши всех файлов с базой — не изменился, идём дальше;
# изменился — переиндексация. Очередь эмбеддингов при этом очищается.
# Фоновая сверка держится в пределах 5% CPU.
#
# Инкрементальный старт без сверки хэшей (только новый/удалённый файл):
#
#   ./run.sh --no-reindex
#   NO_REINDEX=1 ./run.sh
#
# Обычные флаги пробрасываются в `persinder serve` как есть:
#   ./run.sh --config PATH        — явный конфиг
#   ./run.sh --home PATH          — каталог приложения
#   ./run.sh --host 0.0.0.0 --port 8756
#
# Лимиты CPU (чтобы индексация не вешала ПК), в процентах:
#
#   PERSINDER_CPU_LIMIT=30 ./run.sh          # эмбеддинги: не больше 30% ядер
#   PERSINDER_REINDEX_CPU_LIMIT=5 ./run.sh   # сверка хэшей (по умолчанию 5%)
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

# Полная переиндексация по умолчанию; выключить: --no-reindex / NO_REINDEX=1.
reindex="1"
filtered=()
for arg in "$@"; do
    case "$arg" in
        --no-reindex) reindex="0" ;;
        --reindex)    reindex="1" ;;
        *)            filtered+=("$arg") ;;
    esac
done
set -- "${filtered[@]}"
if [ "${NO_REINDEX:-0}" = "1" ] || [ "${REINDEX:-}" = "0" ]; then
    reindex="0"
fi
if [ "$reindex" = "1" ]; then
    set -- --reindex "$@"
fi

exec "$PY" -m persinder serve "$@"
