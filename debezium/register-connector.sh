#!/bin/sh
# Регистрирует (или обновляет) Debezium-коннектор CRM в Kafka Connect.
set -e
CONNECT_URL="${CONNECT_URL:-http://kafka-connect:8083}"
CONFIG_FILE="${CONFIG_FILE:-/debezium/crm-connector.json}"

echo "waiting for Kafka Connect at ${CONNECT_URL}..."
until curl -fsS "${CONNECT_URL}/connectors" >/dev/null 2>&1; do sleep 3; done

NAME=$(sed -n 's/.*"name": *"\([^"]*\)".*/\1/p' "${CONFIG_FILE}" | head -1)
# PUT /connectors/<name>/config идемпотентен: создает или обновляет коннектор
sed -n '/"config": *{/,/^  }/p' "${CONFIG_FILE}" | sed '1s/.*"config": *{/{/; $s/^  }/}/' > /tmp/config.json
curl -fsS -X PUT -H "Content-Type: application/json" \
     --data @/tmp/config.json "${CONNECT_URL}/connectors/${NAME}/config"
echo
sleep 5
curl -fsS "${CONNECT_URL}/connectors/${NAME}/status"
echo
