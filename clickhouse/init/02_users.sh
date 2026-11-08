#!/bin/bash
# Сервисные пользователи ClickHouse с минимальными правами
set -e
clickhouse client -n --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" <<SQL
CREATE USER IF NOT EXISTS airflow IDENTIFIED WITH sha256_password BY '${CLICKHOUSE_AIRFLOW_PASSWORD}';
GRANT SELECT, INSERT, ALTER DELETE, OPTIMIZE ON reports.* TO airflow;

CREATE USER IF NOT EXISTS reports_api IDENTIFIED WITH sha256_password BY '${CLICKHOUSE_REPORTS_API_PASSWORD}';
GRANT SELECT ON reports.* TO reports_api;
SQL
