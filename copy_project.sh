#!/bin/bash
# Copies all project files to clipboard as formatted text

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT=""

FILES=(
  "docker-compose.yml"
  ".env.example"
  "bot/requirements.txt"
  "bot/Dockerfile"
  "bot/main.py"
  "bot/db.py"
  "bot/btc.py"
  "bot/scheduler.py"
  "bot/handlers.py"
)

for f in "${FILES[@]}"; do
  FULL="$PROJECT_DIR/$f"
  if [ -f "$FULL" ]; then
    OUTPUT+="=== $f ===\n"
    OUTPUT+="$(cat "$FULL")\n\n"
  else
    OUTPUT+="=== $f === (NOT FOUND)\n\n"
  fi
done

echo -e "$OUTPUT" | pbcopy
echo "✅ Скопировано в буфер обмена (${#FILES[@]} файлов)"
