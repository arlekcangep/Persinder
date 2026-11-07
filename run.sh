#!/usr/bin/env bash
# run.sh — единый сервис Persinder: HTTP + watcher + эмбеддинги.
#
#   ./run.sh                        — конфиг: папка запуска, затем ~/.persinder
#                                     (persinder.yaml, затем persinder.yml)
#   ./run.sh --config PATH          — явный путь к конфигу
#   ./run.sh --host 0.0.0.0 --port 8756
#
# Запускает `python -m persinder serve --config ... --home ~/.persinder` — ОДИН
# процесс: HTTP (приём + поиск), watcher (первый проход сразу доиндексирует всё
# новое/недоделанное) и фоновый разбор очереди эмбеддингов. Остановка — Ctrl+C.
#
# Требуется: ./setup.sh уже выполнен (venv .venv + модель).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_DIR="$(pwd)"   # где запустили скрипт — отсюда ищем конфиг по умолчанию
cd "$REPO"

VENV="$REPO/.venv"
if [ -f "$VENV/Scripts/python.exe" ]; then
    PY="$VENV/Scripts/python.exe"     # Windows (Git Bash)
elif [ -f "$VENV/bin/python" ]; then
    PY="$VENV/bin/python"             # Linux/macOS
else
    echo "ОШИБКА: venv не найден: $VENV" >&2
    echo "Сначала запусти ./setup.sh" >&2
    exit 1
fi

# --- разбор аргументов: [--config PATH] [--host HOST] [--port PORT] ---
CONFIG=""
HOST=""
PORT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --config)
            if [ $# -lt 2 ]; then
                echo "ОШИБКА: --config требует путь" >&2
                exit 1
            fi
            CONFIG="$2"
            shift 2
            ;;
        --config=*)
            CONFIG="${1#--config=}"
            shift
            ;;
        --host)
            if [ $# -lt 2 ]; then
                echo "ОШИБКА: --host требует адрес" >&2
                exit 1
            fi
            HOST="$2"
            shift 2
            ;;
        --host=*)
            HOST="${1#--host=}"
            shift
            ;;
        --port)
            if [ $# -lt 2 ]; then
                echo "ОШИБКА: --port требует номер" >&2
                exit 1
            fi
            PORT="$2"
            shift 2
            ;;
        --port=*)
            PORT="${1#--port=}"
            shift
            ;;
        *)
            echo "ОШИБКА: неизвестный аргумент: $1" >&2
            echo "Использование: ./run.sh [--config PATH] [--host HOST] [--port PORT]" >&2
            exit 1
            ;;
    esac
done

# Каталог приложения — ~/.persinder (тот же, что Settings.default() в коде).
# Модели живут в <home>/models. Создаём сразу, чтобы каталог был виден.
PERSINDER_HOME="$HOME/.persinder"
if ! mkdir -p "$PERSINDER_HOME"; then
    echo "ОШИБКА: не могу создать каталог приложения: $PERSINDER_HOME" >&2
    exit 1
fi

# Конфиг: явный (--config) или поиск — папка запуска, затем ~/.persinder.
# В каждом каталоге: persinder.yaml, затем persinder.yml.
if [ -z "$CONFIG" ]; then
    for candidate in \
        "$START_DIR/persinder.yaml" "$START_DIR/persinder.yml" \
        "$PERSINDER_HOME/persinder.yaml" "$PERSINDER_HOME/persinder.yml"
    do
        if [ -f "$candidate" ]; then
            CONFIG="$candidate"
            break
        fi
    done
fi
# Относительный --config — от папки запуска, а не от корня репозитория.
case "$CONFIG" in
    "" | /* | [A-Za-z]:*) ;;
    *) CONFIG="$START_DIR/$CONFIG" ;;
esac
if [ -z "$CONFIG" ] || [ ! -f "$CONFIG" ]; then
    echo "ОШИБКА: конфиг не найден." >&2
    echo "Искал: $START_DIR/persinder.{yaml,yml} и $PERSINDER_HOME/persinder.{yaml,yml}" >&2
    echo "Положи конфиг в $PERSINDER_HOME/persinder.yaml или укажи --config PATH" >&2
    exit 1
fi

echo "Конфиг:   $CONFIG"
echo "Home:     $PERSINDER_HOME  (модели: $PERSINDER_HOME/models)"
echo

ARGS=(serve --config "$CONFIG" --home "$PERSINDER_HOME")
if [ -n "$HOST" ]; then
    ARGS+=(--host "$HOST")
fi
if [ -n "$PORT" ]; then
    ARGS+=(--port "$PORT")
fi

exec "$PY" -m persinder "${ARGS[@]}"
