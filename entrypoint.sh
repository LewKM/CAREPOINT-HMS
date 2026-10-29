#!/bin/bash
set -e

echo "=========================================="
echo "🚀 CAREPOINT-HMS container starting..."
echo "=========================================="

echo ""
echo "📌 Environment Check:"
echo "  - DATABASE_URL: ${DATABASE_URL:+SET}"
echo "  - SECRET_KEY: ${SECRET_KEY:+SET}"
echo "  - HTTPS_ONLY: ${HTTPS_ONLY}"
echo "=========================================="
echo ""

# ============================================================
# Wait for DB (Neon/Postgres) using DATABASE_URL only
# ============================================================
echo "⏳ Checking DB connection using DATABASE_URL..."

MAX_ATTEMPTS=5
ATTEMPT=0

until python -c "from app.db import engine; engine.connect().close()" > /dev/null 2>&1; do
  ATTEMPT=$((ATTEMPT+1))
  echo "🔁 DB not ready (attempt $ATTEMPT/$MAX_ATTEMPTS)..."

  if [ "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]; then
    echo "⚠️ DB not reachable after $MAX_ATTEMPTS attempts. Continuing startup..."
    break
  fi

  sleep 2
done

echo "✅ DB check complete!"

# ============================================================
# Run migrations only if RUN_MIGRATIONS=true
#
# There is a single web container (uvicorn's workers fork after
# this script), so nothing else races it for the migration.
# `alembic upgrade head` is a no-op when nothing is pending.
# ============================================================
if [ "$RUN_MIGRATIONS" = "true" ]; then
  echo "=========================================="
  echo "⚙️  Applying pending migrations..."
  echo "=========================================="
  alembic upgrade head
  echo "✅ Migrations complete!"
else
  echo "⚙️  RUN_MIGRATIONS not set — skipping migrations on this container."
fi

# ============================================================
# Run seeds only if RUN_SEEDS=true (SEED_DEMO=true adds the demo
# accounts — never in production). Safe to re-run.
# ============================================================
if [ "$RUN_SEEDS" = "true" ]; then
  echo "=========================================="
  echo "🌱 Running seeds..."
  echo "=========================================="
  if [ "$SEED_DEMO" = "true" ]; then
    python -m scripts.seed --demo
  else
    python -m scripts.seed
  fi
  echo "✅ Seeds complete!"
else
  echo "🌱 Seeds skipped (RUN_SEEDS not set to 'true')"
fi

echo ""

# ============================================================
# Start Uvicorn
# ============================================================
echo "=========================================="
echo "▶️  Starting Uvicorn server..."
echo "=========================================="

exec "$@"
