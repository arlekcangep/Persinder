#!/usr/bin/env bash
# Создать/обновить venv проекта и поставить зависимости.
#
# venv кладётся вне vboxfs (в ~/opencodetmp): на общей папке нет симлинков, и pip там
# ломается. В контейнере ~/opencodetmp смонтирован как named volume (см. docker-compose).
# ⚠️ Volume временный: при пересоздании VM/тома venv пропадает — просто прогнать заново.
#
# Использование:
#   scripts/venv.sh
#
# Переопределить путь: PERSINDER_VENV=/path/to/venv scripts/venv.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv="${PERSINDER_VENV:-$HOME/opencodetmp/venvs/persinder}"

echo ">> venv: $venv"
python3 -m venv "$venv"

echo ">> обновляю pip"
"$venv/bin/pip" install --quiet --upgrade pip

echo ">> ставлю проект с extras: dev, config, watch, stemming, onnx"
"$venv/bin/pip" install --quiet -e "${here}[dev,config,watch,stemming,onnx]"

echo ">> готово. Активировать: source $venv/bin/activate"
