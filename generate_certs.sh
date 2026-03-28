#!/bin/bash
# Generate self-signed TLS certificates for the WebSocket audio bridge.
# Run this once before starting the bot.

mkdir -p certs
openssl req -x509 -newkey rsa:2048 \
  -keyout certs/localhost-key.pem \
  -out certs/localhost.pem \
  -days 365 -nodes -subj "/CN=localhost"

echo "Certificates generated in certs/"
