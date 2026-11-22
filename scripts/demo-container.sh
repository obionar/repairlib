#!/bin/sh
# Disposable demo inside the RepairLib image: user demo / password demo, mythical seed data.
# The workspace lives in the container and disappears with it. Guide: docs/deployment.md
set -eu
workspace=/tmp/repairlib-demo
if [ ! -e "$workspace/state" ]; then
    printf 'demo\n' | python3 /app/bin/repairlib setup --workspace "$workspace" \
        --shop "The Mythical Fix-It Emporium" --username demo --timezone "${TZ:-Europe/Nicosia}"
    python3 /app/scripts/seed_demo.py --workspace "$workspace" --dataset /app/scripts/demo-mythical.json
fi
exec python3 /app/bin/repairlib service foreground --workspace "$workspace" --port "${PORT:-8000}"
