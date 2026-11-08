#!/bin/bash
# Учебный single-node Airflow: метаданные в PostgreSQL, LocalExecutor,
# scheduler и webserver в одном контейнере.
set -e
airflow db migrate
airflow users create --username "${AIRFLOW_ADMIN_USER:-admin}" --password "${AIRFLOW_ADMIN_PASSWORD:-admin}" \
  --firstname Admin --lastname BionicPRO --role Admin --email admin@example.com >/dev/null 2>&1 || true
airflow scheduler &
exec airflow webserver --port 8080
