#!/usr/bin/env bash
# Залить изменения на GitHub и сразу обновить демо на Render.
# Ссылка Render берётся из .env (RENDER_DEPLOY_HOOK) — в GitHub она не попадает.
set -e
cd "$(dirname "$0")/.."
git push "$@"
HOOK=$(grep '^RENDER_DEPLOY_HOOK=' .env 2>/dev/null | cut -d= -f2- | tr -d '\r')
if [ -n "$HOOK" ]; then
  curl -fsS -X POST "$HOOK" > /dev/null && echo "Демо на Render: сборка запущена (3–5 минут)"
else
  echo "В .env нет RENDER_DEPLOY_HOOK — демо не обновлено"
fi
