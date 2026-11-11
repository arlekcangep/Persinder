# shellcheck shell=bash
# scripts/py.sh — общий выбор интерпретатора Python для обёрток проекта
# (run.sh, deploy.sh). Использование: задать REPO, затем
#
#     source "$REPO/scripts/py.sh"
#
# Задаёт:
#   PY           — путь к python (venv проекта или системный)
#   PY_FROM_VENV — 1, если python найден в venv; 0 — если системный
#
# Порядок кандидатов:
#   1. .venv репозитория (Windows: Scripts/python.exe, POSIX: bin/python)
#   2. $PERSINDER_VENV или ~/venvs/persinder (venv вне общей папки,
#      см. scripts/venv.sh)
#   3. системный python3 (затем python)
#
# Кандидат проверяется фактическим запуском, а не только наличием файла:
# venv, созданный на другой ОС, содержит бинарник, который есть, но не
# исполняется (например, .venv/Scripts/python.exe от Windows на Linux).

py_works() { "$1" -c 'import sys' >/dev/null 2>&1; }

PY=""
# shellcheck disable=SC2034  # PY_FROM_VENV используют скрипты, подключающие этот файл
PY_FROM_VENV=0

for cand in \
    "$REPO/.venv/Scripts/python.exe" \
    "$REPO/.venv/bin/python" \
    "${PERSINDER_VENV:-$HOME/venvs/persinder}/bin/python" \
    "${PERSINDER_VENV:-$HOME/venvs/persinder}/Scripts/python.exe"; do
    if [ -f "$cand" ] && py_works "$cand"; then
        PY="$cand"
        # shellcheck disable=SC2034  # используется подключающими скриптами
        PY_FROM_VENV=1
        break
    fi
done

if [ -z "$PY" ]; then
    PY="$(command -v python3 || command -v python || true)"
fi
