#!/bin/sh
set -eu

workspace=/workspace
config=/workspace/config/default.json

if [ ! -f "$config" ]; then
    if [ -e "$config" ]; then
        echo "ERROR: $config exists but is not a regular file" >&2
        exit 1
    fi
    echo "Initializing AI Trade Docker workspace in $workspace"
    ai-trade init --directory "$workspace"
fi

exec ai-trade --config "$config" "$@"
