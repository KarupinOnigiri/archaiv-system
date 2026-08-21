#!/usr/bin/env bash
# One-shot setup for a fresh Raspberry Pi deployment -- see DEPLOYMENT.md
# for the full walkthrough this automates (and for what to do instead if
# something here fails and you'd rather work through it by hand).
#
# Installs system + Python dependencies (including SQLCipher, so the
# database is encrypted at rest from the very first run -- see
# SECURITY.md for what that does and doesn't actually protect against
# before relying on it), generates .env with fresh secrets, and hardens
# permissions. Does NOT install/start the systemd service -- that's a
# separate, deliberate step (DEPLOYMENT.md step 6) so you can sanity-check
# a first manual run before it's wired up to auto-start.
#
# Safe to re-run: skips steps that already look done rather than
# clobbering an existing venv/.env.
#
# Usage:
#   ./deploy/setup.sh                 # encrypted database (default)
#   ./deploy/setup.sh --no-encryption # skip SQLCipher entirely --
#                                      # faster setup, no compile step,
#                                      # but see SECURITY.md's Layer 1
#                                      # (file permissions, still applied
#                                      # either way) for what you're
#                                      # relying on instead
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
cd "$APP_DIR"

ENCRYPT=1
for arg in "$@"; do
  case "$arg" in
    --no-encryption) ENCRYPT=0 ;;
    *)
      echo "Unknown argument: $arg (only --no-encryption is supported)" >&2
      exit 1
      ;;
  esac
done

echo "==> Installing system packages (needs sudo)..."
if [ "$ENCRYPT" = "1" ]; then
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip \
    libsqlcipher-dev libssl-dev build-essential python3-dev
else
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip
fi

echo "==> Setting up the virtual environment..."
if [ ! -d "$APP_DIR/.venv" ]; then
  python3 -m venv "$APP_DIR/.venv"
else
  echo "    .venv already exists, reusing it."
fi

VENV_PIP="$APP_DIR/.venv/bin/pip"
VENV_PY="$APP_DIR/.venv/bin/python"

echo "==> Installing Python dependencies..."
"$VENV_PIP" install --upgrade pip --quiet
"$VENV_PIP" install -r "$APP_DIR/requirements.txt" --quiet
if [ "$ENCRYPT" = "1" ]; then
  echo "    Installing sqlcipher3 -- this compiles from source, expect a"
  echo "    few minutes on a Pi, not seconds (no prebuilt wheel exists)."
  "$VENV_PIP" install -r "$APP_DIR/requirements-encryption.txt"
fi

echo "==> Configuring .env..."
if [ -f "$APP_DIR/.env" ]; then
  echo "    .env already exists -- leaving it alone. Delete it first if"
  echo "    you want this script to regenerate it from scratch."
else
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"

  SECRET_KEY_VALUE="$("$VENV_PY" -c 'import secrets; print(secrets.token_hex(32))')"
  # sed -i differs between GNU sed (Linux/Pi) and BSD sed (macOS) -- this
  # targets the Pi specifically, so plain GNU-style -i is fine here.
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET_KEY_VALUE}|" "$APP_DIR/.env"

  # Plain HTTP by default (no reverse proxy in front of this script's
  # setup) -- see .env.example's own comment on COOKIE_SECURE for why
  # getting this backwards silently breaks login with no visible error.
  # .env.example already ships COOKIE_SECURE=0, so nothing to change here
  # unless you've since put a TLS reverse proxy in front of the app --
  # if so, edit .env afterward and set it to 1.

  if [ "$ENCRYPT" = "1" ]; then
    DB_KEY_VALUE="$("$VENV_PY" -c 'import secrets; print(secrets.token_hex(32))')"
    if grep -q '^# DB_ENCRYPTION_KEY=' "$APP_DIR/.env"; then
      sed -i "s|^# DB_ENCRYPTION_KEY=.*|DB_ENCRYPTION_KEY=${DB_KEY_VALUE}|" "$APP_DIR/.env"
    else
      echo "DB_ENCRYPTION_KEY=${DB_KEY_VALUE}" >> "$APP_DIR/.env"
    fi
    echo ""
    echo "    Generated a database encryption key and wrote it to .env."
    echo "    BACK THIS UP SOMEWHERE OTHER THAN THIS SD CARD -- if the"
    echo "    card fails and you don't have it, the database is"
    echo "    permanently unreadable, encryption doesn't have a"
    echo "    recovery path by design. It's also in .env right now:"
    echo ""
    echo "        ${DB_KEY_VALUE}"
    echo ""
  fi

  chmod 600 "$APP_DIR/.env"
  echo "    Wrote .env with a fresh SECRET_KEY$([ "$ENCRYPT" = "1" ] && echo " and DB_ENCRYPTION_KEY")."
fi

echo "==> Running the app once to create the database and apply migrations..."
# init_db() (database.py) runs automatically on import -- this also
# exercises harden_data_permissions() so instance/ and the DB file end up
# with the right permissions before the systemd service ever touches
# them. Timeout as a safety net in case something hangs waiting on
# stdin/a port that's already in use -- shouldn't happen, but this
# shouldn't block a first-time setup run indefinitely either.
#
# load_dotenv() FIRST, exactly like app.py does, before `import database`
# -- database.py reads DB_ENCRYPTION_KEY from the environment at import
# time, not at call time. Skipping this here (as an earlier version of
# this script did) meant this check created the database with encryption
# silently OFF regardless of what .env said, since the key was never
# actually loaded into the environment -- then the real app (which does
# load .env correctly) would fail to open that same file, since it's
# actually plaintext despite DB_ENCRYPTION_KEY being set.
timeout 15 "$VENV_PY" -c "
from dotenv import load_dotenv
load_dotenv('.env')
import database
database.init_db()
print('Database ready at', database.DB_PATH)
print('Encryption:', 'ON' if database.DB_ENCRYPTION_KEY else 'off')
" || {
  echo "    (non-fatal) Database init check timed out or errored --"
  echo "    this can happen harmlessly depending on environment; verify"
  echo "    manually with: .venv/bin/python wsgi.py"
}

echo ""
echo "==> Done. Next steps (DEPLOYMENT.md has the full detail on each):"
echo "    1. Review .env -- especially FLASK_HOST/FLASK_PORT and"
echo "       COOKIE_SECURE if you're putting a reverse proxy in front."
echo "    2. Sanity-check a manual run: .venv/bin/python wsgi.py"
echo "    3. Install the systemd service (DEPLOYMENT.md step 6)."
