#!/usr/bin/env bash
set -Eeuo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -f .env ]]; then
  echo "Ошибка: создайте .env из .env.example и заполните секреты." >&2
  exit 1
fi
chmod 600 .env

env_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $0 !~ /^[[:space:]]*#/ && $1 == key {
      sub(/^[^=]*=/, "")
      gsub(/^[[:space:]\"]+|[[:space:]\"]+$/, "")
      print
      exit
    }
  ' .env
}

bot_token="$(env_value BOT_TOKEN)"
chat_id="$(env_value TELEGRAM_CHAT_ID)"
bot_token_lc="${bot_token,,}"
chat_id_lc="${chat_id,,}"
if [[ -z "$bot_token" || "$bot_token_lc" =~ replace_me|changeme|example ]]; then
  echo "Ошибка: BOT_TOKEN отсутствует или содержит placeholder." >&2
  exit 1
fi
if [[ -z "$chat_id" || "$chat_id" == "-1001234567890" \
  || "$chat_id_lc" =~ replace_me|changeme|example ]]; then
  echo "Ошибка: TELEGRAM_CHAT_ID отсутствует или содержит placeholder." >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Ошибка: Docker не установлен." >&2
  exit 1
fi

mkdir -p data backups
chmod 700 data backups
export COMPOSE_PROJECT_NAME=binance-futures-short-bot

exec 9>.deploy.lock
if ! flock -n 9; then
  echo "Другая операция deploy/update уже выполняется." >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
legacy_state="signals_state.json"
current_state="data/signals_state.json"
candidate_tag="deploy-${timestamp}"
export DEPLOY_IMAGE_TAG="$candidate_tag"
candidate_image="binance-futures-short-bot:${candidate_tag}"
service_stopped=0
rollback_armed=0
deploy_succeeded=0
old_image_id="$(
  docker inspect --format '{{.Image}}' binance-futures-short-bot 2>/dev/null || true
)"

rollback_on_error() {
  local exit_code=$?
  local running=""
  trap - ERR
  if (( rollback_armed == 1 && deploy_succeeded == 0 )); then
    running="$(
      docker inspect --format '{{.State.Running}}' \
        binance-futures-short-bot 2>/dev/null || true
    )"
    if (( service_stopped == 1 )) || [[ "$running" != "true" ]]; then
      echo "Deploy не завершён; выполняется rollback предыдущего контейнера." >&2
      if [[ -n "$old_image_id" ]]; then
        docker image tag "$old_image_id" "$candidate_image" || true
        docker compose up -d --no-build --force-recreate \
          --remove-orphans --scale bot=1 || true
      else
        docker compose start bot || true
      fi
    else
      echo "Deploy остановлен до downtime; предыдущий контейнер продолжает работу." >&2
    fi
  fi
  exit "$exit_code"
}
trap rollback_on_error ERR

normalize_owner() {
  if chown -R 1000:1000 "$@" 2>/dev/null; then
    return
  fi
  if command -v sudo >/dev/null 2>&1 \
    && sudo -n chown -R 1000:1000 "$@"; then
    return
  fi
  echo "Ошибка: не удалось назначить UID/GID 1000 владельцем state." >&2
  return 1
}

safe_permissions() {
  if chmod 755 data 2>/dev/null \
    && find data -type f -exec chmod 600 {} + 2>/dev/null; then
    return
  fi
  if command -v sudo >/dev/null 2>&1 \
    && sudo -n chmod 755 data \
    && sudo -n find data -type f -exec chmod 600 {} +; then
    return
  fi
  echo "Ошибка: не удалось установить безопасные permissions state." >&2
  return 1
}

validate_state() {
  if python3 state_preflight.py "$1"; then
    return
  fi
  if command -v sudo >/dev/null 2>&1 \
    && sudo -n python3 state_preflight.py "$1"; then
    return
  fi
  echo "Ошибка: state $1 не прошёл schema validation." >&2
  return 1
}

backup_state() {
  local source="$1"
  local destination="$2"
  if cp -p "$source" "$destination" 2>/dev/null; then
    return
  fi
  if command -v sudo >/dev/null 2>&1 \
    && sudo -n cp -p "$source" "$destination"; then
    return
  fi
  echo "Ошибка: не удалось создать backup $source." >&2
  return 1
}

check_state_layout() {
  if [[ -f "$legacy_state" && -f "$current_state" ]]; then
    backup_state "$legacy_state" "backups/legacy-conflict.${timestamp}.json"
    backup_state "$current_state" "backups/current-conflict.${timestamp}.json"
    echo "Ошибка: найдены оба state-файла. Созданы backups; выберите один вручную." >&2
    return 1
  fi
}

# All failure-prone preflight work happens while the old container is running.
docker compose config --quiet
check_state_layout
[[ ! -f "$legacy_state" ]] || validate_state "$legacy_state"
[[ ! -f "$current_state" ]] || validate_state "$current_state"
normalize_owner data
safe_permissions
docker compose build bot
docker compose run --rm --no-deps --entrypoint python bot -c \
  "from pathlib import Path; p=Path('/app/data/.deploy-write-test'); q=p.with_suffix('.new'); p.write_text('ok'); q.write_text('ok'); q.replace(p); p.unlink()"

# Stop only for the consistent backup/migration and prebuilt-image replacement.
rollback_armed=1
docker compose stop bot
service_stopped=1
check_state_layout
[[ ! -f "$legacy_state" ]] || validate_state "$legacy_state"
[[ ! -f "$current_state" ]] || validate_state "$current_state"

if [[ -f "$legacy_state" ]]; then
  backup_state "$legacy_state" "backups/legacy-state.${timestamp}.json"
  if ! mv "$legacy_state" "$current_state" 2>/dev/null; then
    if ! command -v sudo >/dev/null 2>&1 \
      || ! sudo -n mv "$legacy_state" "$current_state"; then
      echo "Ошибка: не удалось перенести legacy state." >&2
      false
    fi
  fi
elif [[ -f "$current_state" ]]; then
  backup_state "$current_state" "backups/signals_state.${timestamp}.json"
fi

normalize_owner data
safe_permissions
docker compose up -d --no-build --force-recreate --remove-orphans --scale bot=1

deadline=$((SECONDS + ${DEPLOY_HEALTH_TIMEOUT:-900}))
while (( SECONDS < deadline )); do
  status="$(
    docker inspect \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
      binance-futures-short-bot 2>/dev/null || true
  )"
  if [[ "$status" == "healthy" ]]; then
    deploy_succeeded=1
    service_stopped=0
    rollback_armed=0
    docker image tag "$candidate_image" binance-futures-short-bot:latest
    docker compose ps
    exit 0
  fi
  running="$(
    docker inspect --format '{{.State.Running}}' \
      binance-futures-short-bot 2>/dev/null || true
  )"
  if [[ "$running" == "false" ]]; then
    break
  fi
  sleep 5
done

echo "Ошибка: бот не достиг состояния healthy." >&2
docker compose ps
docker compose logs --tail=200 bot >&2
false
