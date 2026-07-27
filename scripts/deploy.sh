#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_DIR="/opt/soul/guild"
readonly BRANCH="guild"
readonly SERVICE="soul-bot.service"

cd "$APP_DIR"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Deployment refused: tracked files have local changes." >&2
    exit 1
fi

previous_commit="$(git rev-parse HEAD)"

rollback() {
    local exit_code=$?
    trap - ERR
    echo "Deployment failed; rolling back to ${previous_commit}." >&2
    git reset --hard "$previous_commit"
    sudo -n systemctl restart "$SERVICE" || true
    exit "$exit_code"
}
trap rollback ERR

git fetch --prune origin "$BRANCH"
git checkout "$BRANCH"

if [[ "$(git rev-parse HEAD)" == "$(git rev-parse "origin/$BRANCH")" ]]; then
    trap - ERR
    echo "Already up to date at $(git rev-parse --short HEAD)."
    exit 0
fi

git merge --ff-only "origin/$BRANCH"

if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi

.venv/bin/python -m pip install --disable-pip-version-check --upgrade pip
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
.venv/bin/python -m compileall -q -x '(^|/)(\.git|\.venv|python-embed)(/|$)' .

sudo -n systemctl restart "$SERVICE"

for attempt in {1..20}; do
    if curl --fail --silent --show-error http://127.0.0.1:8080/healthz >/dev/null; then
        trap - ERR
        echo "Deployed $(git rev-parse --short HEAD) successfully."
        exit 0
    fi
    sleep 2
done

echo "Health check failed after 40 seconds." >&2
false
