#!/usr/bin/env bash
# Создать/обновить venv проекта и поставить зависимости.
#
# venv кладётся вне vboxfs (в ~/venvs): на общей папке нет симлинков, и pip там
# ломается. В контейнере home смонтирован как named volume (см. docker-compose).
# ⚠️ Volume временный: при пересоздании VM/тома venv пропадает — просто прогнать заново.
#
# Использование:
#   scripts/venv.sh
#
# Переопределить путь: PERSINDER_VENV=/path/to/venv scripts/venv.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv="${PERSINDER_VENV:-$HOME/venvs/persinder}"

echo ">> venv: $venv"
python3 -m venv "$venv"

echo ">> обновляю pip"
"$venv/bin/pip" install --quiet --upgrade pip

echo ">> ставлю проект с extras (dev + все экстракторы для тестов)"
"$venv/bin/pip" install --quiet -e "${here}[dev,config,watch,stemming,pdf,archive7z,archiverar,http]"

echo ">> готово. Активировать: source $venv/bin/activate"
