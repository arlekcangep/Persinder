#!/usr/bin/env bash
# setup.sh — установка Persinder в локальный venv (Git Bash / Windows).
#
#   ./setup.sh              — установка + индексация test-content + поиск
#   ./setup.sh --install    — только установка (без запуска)
#
# Что делает:
#   1. Проверяет Python 3.10+ и создаёт venv в корне репозитория (.venv).
#   2. Ставит persinder со всеми extra (pdf, archive7z, archiverar, config,
#      watch, http, stemming).
#   3. Скачивает модель эмбеддингов e5-small-int8-v1 (~120 МБ) в каталог
#      приложения ~/.persinder/models (модели всегда в <home>/models).
#   4. Показывает, как запустить индексацию test-content.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

VENV="$REPO/.venv"

# Найти живой python: на Windows 'python' бывает программной оболочкой
# (Microsoft Store alias, MSYS-обёртка), которая не является настоящим
# интерпретатором. Проверяем фактическим запуском, а не по имени/пути:
# заглушка падает, настоящий Python (хоть из WindowsApps, хоть с python.org)
# выполняет код.
find_python() {
    for cand in python3.exe python.exe python3 py.exe py; do
        command -v "$cand" >/dev/null 2>&1 || continue
        if "$cand" -c 'import sys' >/dev/null 2>&1; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

if ! BASEPY="$(find_python)"; then
    echo "ОШИБКА: не нашёл работающий python в PATH." >&2
    echo "Если Python установлен — добавь его в PATH или укажи явно:" >&2
    echo "    PATH=\"/c/Program Files/Python314:\$PATH\" ./setup.sh   # пример" >&2
    echo "Если не установлен — поставь с python.org (отметь 'Add Python to PATH')." >&2
    exit 1
fi
pyver=$("$BASEPY" -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))')
echo "Python: $pyver ($BASEPY)"
"$BASEPY" - "$pyver" <<'EOF'
import sys

version = sys.argv[1]
major, minor = (int(p) for p in version.split("."))
if (major, minor) < (3, 10):
    sys.exit("ОШИБКА: нужен Python >= 3.10, найден " + version)
EOF
echo "OK"

# --- venv ---
# Путь к python внутри venv различается: Windows — Scripts/python.exe,
# POSIX — bin/python. Определяем ПОСЛЕ создания (или находим существующий).
venv_python() {
    if [ -f "$VENV/Scripts/python.exe" ]; then
        echo "$VENV/Scripts/python.exe"
    elif [ -f "$VENV/bin/python" ]; then
        echo "$VENV/bin/python"
    else
        echo ""
    fi
}

PY="$(venv_python)"
if [ -z "$PY" ]; then
    echo "Создаю venv: $VENV"
    "$BASEPY" -m venv "$VENV"
    PY="$(venv_python)"
fi

if [ -z "$PY" ]; then
    echo "ОШИБКА: venv создан, но python внутри не найден: $VENV" >&2
    exit 1
fi
echo "Venv python: $PY"

# --- установка пакета со всеми extra ---
echo "Ставлю persinder + все extra..."
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -e ".[config,watch,http,pdf,archive7z,archiverar,stemming]"
echo "OK: persinder установлен."

# --- модель эмбеддингов ---
# Модели всегда лежат в <home>/models; по умолчанию home = ~/.persinder.
if "$PY" -m persinder model list 2>/dev/null | grep -q "e5-small-int8-v1"; then
    echo "Модель e5-small-int8-v1 уже установлена."
else
    echo "Качаю модель e5-small-int8-v1 (~120 МБ) -> ~/.persinder/models ..."
    "$PY" -m persinder model install e5-small-int8-v1
    "$PY" -m persinder embed --background
    "$PY" -m persinder model use e5-small-int8-v1
fi

# --- готово ---
cat <<'EOF'

Установка завершена.

Запуск сервиса (Git Bash):

    ./run.sh                          — конфиг: папка запуска, затем ~/.persinder
    ./run.sh --config /путь/persinder.yaml
    ./run.sh --host 0.0.0.0 --port 8756   — доступ с других машин

Конфиг по умолчанию ищется как persinder.yaml (затем .yml) в папке запуска
и в ~/.persinder. Модели и БД живут в ~/.persinder.

Вручную (в отдельном терминале):

    source .venv/Scripts/activate     # Windows Git Bash

    persinder serve --config ../test-content/persinder.yaml
    persinder search "история поиска видео"

Веб-интерфейс: http://127.0.0.1:8756/ — уже собран и раздаётся сервером,
Node.js и npm не нужны.
EOF
