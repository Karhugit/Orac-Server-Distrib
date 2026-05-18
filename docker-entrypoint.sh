#!/bin/sh
# docker-entrypoint.sh
# Bootstraps config.json from config.example.json on first run,
# then starts the Orac Server.

if [ ! -f /app/config.json ] && [ -f /app/config.example.json ]; then
    echo "config.json not found - copying from config.example.json..."
    cp /app/config.example.json /app/config.json
    echo "NOTE: Default API keys are pre-configured. Edit config.json to customise."
fi

exec python run_server.py
