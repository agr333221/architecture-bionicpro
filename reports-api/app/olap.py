"""Запросы к витринам ClickHouse.

Все тяжелые вычисления сделаны заранее (Airflow DAG или MaterializedView),
здесь только выборка по первичному ключу витрины (customer -> date -> prosthesis).
"""
from datetime import date, datetime, timezone

import clickhouse_connect

from .config import settings

_client = None


def client():
    global _client
    if _client is None:
        _client = clickhouse_connect.get_client(
            host=settings.ch_host,
            port=settings.ch_port,
            username=settings.ch_user,
            password=settings.ch_password,
            database=settings.ch_database,
        )
    return _client


def processed_until() -> datetime | None:
    """Граница данных, уже обработанных ETL (watermark Airflow)."""
    value = client().command("SELECT max(processed_until) FROM reports.etl_watermark")
    if not value or str(value).startswith("1970-01-01"):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)


def _rows(result) -> list[dict]:
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


# --------------------------------------------------------------------------
# Витрина Airflow (задание 2): reports.report_daily
# --------------------------------------------------------------------------
_AIRFLOW_CUSTOMER = """
SELECT customer_id, customer_email AS email, full_name, country
FROM reports.report_daily FINAL
WHERE customer_email = {email:String}
ORDER BY report_date DESC
LIMIT 1
"""

_AIRFLOW_DAILY = """
SELECT
    report_date, prosthesis_serial, prosthesis_model,
    events_count, movements_count, errors_count, slow_reactions_count,
    round(avg_reaction_ms, 1) AS avg_reaction_ms,
    round(p95_reaction_ms, 1) AS p95_reaction_ms,
    max_reaction_ms,
    round(avg_signal_amplitude, 3) AS avg_signal_amplitude,
    min_battery_level,
    round(avg_battery_level, 1) AS avg_battery_level
FROM reports.report_daily FINAL
WHERE customer_email = {email:String}
  AND report_date BETWEEN {date_from:Date} AND {date_to:Date}
ORDER BY report_date, prosthesis_serial
"""

# --------------------------------------------------------------------------
# Витрина CDC (задание 4): reports.report_daily_cdc (AggregatingMergeTree, MV)
# --------------------------------------------------------------------------
_CDC_CUSTOMER = """
SELECT customer_id, email, full_name, country
FROM reports.crm_customers_cdc FINAL
WHERE email = {email:String} AND is_deleted = 0
LIMIT 1
"""

_CDC_DAILY = """
SELECT
    r.report_date AS report_date,
    r.prosthesis_serial AS prosthesis_serial,
    any(p.model) AS prosthesis_model,
    sum(r.events_count) AS events_count,
    sum(r.movements_count) AS movements_count,
    sum(r.errors_count) AS errors_count,
    sum(r.slow_reactions_count) AS slow_reactions_count,
    round(avgMerge(r.avg_reaction_ms), 1) AS avg_reaction_ms,
    round(quantileMerge(0.95)(r.p95_reaction_ms), 1) AS p95_reaction_ms,
    max(r.max_reaction_ms) AS max_reaction_ms,
    round(avgMerge(r.avg_signal_amplitude), 3) AS avg_signal_amplitude,
    min(r.min_battery_level) AS min_battery_level,
    round(avgMerge(r.avg_battery_level), 1) AS avg_battery_level
FROM reports.report_daily_cdc AS r
LEFT JOIN (
    SELECT serial_number, model FROM reports.crm_prostheses_cdc FINAL
    WHERE customer_id = {customer_id:UInt32} AND is_deleted = 0
) AS p ON p.serial_number = r.prosthesis_serial
WHERE r.customer_id = {customer_id:UInt32}
  AND r.report_date BETWEEN {date_from:Date} AND {date_to:Date}
GROUP BY r.report_date, r.prosthesis_serial
ORDER BY report_date, prosthesis_serial
"""


def load_report_rows(email: str, date_from: date, date_to: date) -> tuple[dict | None, list[dict]]:
    params = {"email": email, "date_from": date_from, "date_to": date_to}
    c = client()
    if settings.report_source == "cdc":
        customers = _rows(c.query(_CDC_CUSTOMER, parameters=params))
        if not customers:
            return None, []
        customer = customers[0]
        params["customer_id"] = customer["customer_id"]
        return customer, _rows(c.query(_CDC_DAILY, parameters=params))

    customers = _rows(c.query(_AIRFLOW_CUSTOMER, parameters=params))
    if not customers:
        return None, []
    return customers[0], _rows(c.query(_AIRFLOW_DAILY, parameters=params))
