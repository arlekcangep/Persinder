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
#   3. Скачивает модель эмбеддингов e5-small-int8-v1 (~120 МБ) в общий каталог
#      ~/.persinder/models (модель общая для профилей, вне репозитория).
#   4. Показывает, как запустить индексацию test-content.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

VENV="$REPO/.venv"
PY="$VENV/Scripts/python.exe"     # Windows (Git Bash)
[ -f "$PY" ] || PY="$VENV/bin/python"  # Linux/macOS

# --- python >= 3.10 ---
if ! command -v python >/dev/null 2>&1; then
    echo "ОШИБКА: python не найден в PATH. Установи Python 3.10+ с python.org" >&2
    echo "(при установке отметь 'Add Python to PATH')" >&2
    exit 1
fi
pyver=$(python -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))')
echo "Python: $pyver"
python - "$pyver" <<'EOF'
import sys

version = sys.argv[1]
major, minor = (int(p) for p in version.split("."))
if (major, minor) < (3, 10):
    sys.exit("ОШИБКА: нужен Python >= 3.10, найден " + version)
EOF
echo "OK"

# --- venv ---
if [ ! -f "$PY" ]; then
    echo "Создаю venv: $VENV"
    python -m venv "$VENV"
fi

# --- установка пакета со всеми extra ---
echo "Ставлю persinder + все extra..."
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -e ".[config,watch,http,pdf,archive7z,archiverar,stemming]"
echo "OK: persinder установлен."

# --- модель эмбеддингов ---
# PERSINDER_MODEL_DIR — общий каталог моделей (вне репозитория и venv).
export PERSINDER_MODEL_DIR="${PERSINDER_MODEL_DIR:-$HOME/.persinder/models}"

if "$PY" -m persinder model list 2>/dev/null | grep -q "e5-small-int8-v1"; then
    echo "Модель e5-small-int8-v1 уже установлена."
else
    echo "Качаю модель e5-small-int8-v1 (~120 МБ) -> $PERSINDER_MODEL_DIR ..."
    "$PY" -m persinder model install e5-small-int8-v1
    "$PY" -m persinder embed --background
    "$PY" -m persinder model use e5-small-int8-v1
fi

# --- готово ---
cat <<'EOF'

Установка завершена.

Запуск индексации test-content (Git Bash):

    ./run.sh          — watcher в фоне + разбор очереди эмбеддингов + поиск
    ./run.sh "запрос" — то же + поиск по запросу

Вручную (в отдельном терминале):

    source .venv/Scripts/activate     # Windows Git Bash
    export PERSINDER_MODEL_DIR=~/.persinder/models

    persinder watch --config ../test-content/persinder.yaml
    persinder embed --background
    persinder search "история поиска видео"
EOF
