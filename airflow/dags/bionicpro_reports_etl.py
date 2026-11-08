"""BionicPRO: ETL телеметрии и CRM в OLAP (ClickHouse) и построение витрины отчетов.

Расписание: каждый час (в начале часа). Один прогон обрабатывает данные,
поступившие на сервер в интервале (последний watermark, data_interval_end].
Ручной запуск до текущего момента: airflow dags trigger bionicpro_reports_etl -c '{"until": "now"}'

Задачи:
  extract_window      - вычисляет интервал инкрементальной загрузки по watermark
  load_crm            - снимок CRM (клиенты, протезы) -> reports.crm_*         [режим airflow]
  load_telemetry      - новая телеметрия батчами -> reports.telemetry_raw
  build_report_mart   - пересчет витрины reports.report_daily за затронутые дни [режим airflow]
  commit_watermark    - фиксирует границу обработанных данных (reports.etl_watermark)

Режим CRM задается переменной Airflow `crm_source`:
  airflow (по умолчанию) - CRM выгружается из PostgreSQL этим DAG-ом (задание 2);
  cdc - CRM приходит в ClickHouse через Debezium/Kafka, витрину строит MaterializedView,
        DAG загружает только телеметрию и не нагружает БД CRM (задание 4).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import clickhouse_connect
from airflow import DAG
from airflow.models import Variable
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

log = logging.getLogger(__name__)

BATCH_SIZE = 20_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

TELEMETRY_COLUMNS = [
    "event_id", "prosthesis_serial", "ts", "received_at", "movement_type",
    "signal_amplitude", "reaction_ms", "battery_level", "error_code",
]


def ch_client():
    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "clickhouse"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "airflow"),
        password=os.environ["CLICKHOUSE_PASSWORD"],
        database="reports",
    )


def crm_source() -> str:
    return Variable.get("crm_source", default_var=os.getenv("CRM_SOURCE", "airflow"))


# ---------------------------------------------------------------------------
def extract_window(data_interval_end=None, dag_run=None, **_) -> dict:
    last = ch_client().command("SELECT max(processed_until) FROM reports.etl_watermark")
    low = EPOCH
    if last and not str(last).startswith("1970-01-01"):
        low = last if isinstance(last, datetime) else datetime.fromisoformat(str(last))
        low = low.replace(tzinfo=timezone.utc)
    high = data_interval_end.astimezone(timezone.utc).replace(microsecond=0)
    # Ручной запуск с conf {"until": "now"} обрабатывает данные до текущего момента
    if dag_run is not None and (dag_run.conf or {}).get("until") == "now":
        high = datetime.now(timezone.utc).replace(microsecond=0)
    if high < low:
        high = low
    log.info("incremental window: (%s, %s]", low, high)
    return {"low": low.isoformat(), "high": high.isoformat()}


def load_crm(**_) -> None:
    if crm_source() == "cdc":
        log.info("crm_source=cdc: CRM arrives via Debezium/Kafka, skip bulk export from CRM DB")
        return
    hook = PostgresHook(postgres_conn_id="crm_db")
    with hook.get_conn() as conn:
        # Короткая read-only транзакция с согласованным снимком данных
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn.cursor() as cur:
            cur.execute("SELECT id, lower(email), full_name, country, updated_at FROM customers")
            customers = cur.fetchall()
            cur.execute("""SELECT id, customer_id, serial_number, model, side, status,
                                  installed_at, updated_at FROM prostheses""")
            prostheses = cur.fetchall()
    ch = ch_client()
    ch.insert("crm_customers", customers,
              column_names=["customer_id", "email", "full_name", "country", "updated_at"])
    ch.insert("crm_prostheses", prostheses,
              column_names=["prosthesis_id", "customer_id", "serial_number", "model", "side",
                            "status", "installed_at", "updated_at"])
    log.info("CRM snapshot loaded: %d customers, %d prostheses", len(customers), len(prostheses))


def load_telemetry(ti=None, **_) -> int:
    window = ti.xcom_pull(task_ids="extract_window")
    hook = PostgresHook(postgres_conn_id="telemetry_db")
    ch = ch_client()
    total = 0
    with hook.get_conn() as conn:
        conn.set_session(readonly=True)
        # Серверный курсор: не тянем все строки в память
        with conn.cursor(name="telemetry_export") as cur:
            cur.itersize = BATCH_SIZE
            cur.execute(
                f"""SELECT {', '.join(c if c != 'event_id' else 'id' for c in TELEMETRY_COLUMNS)}
                    FROM telemetry_events
                    WHERE received_at > %(low)s AND received_at <= %(high)s
                    ORDER BY received_at, id""",
                window,
            )
            batch_no = 0
            while True:
                rows = cur.fetchmany(BATCH_SIZE)
                if not rows:
                    break
                # Токен дедупликации: при retry задачи тот же батч не задвоится
                # (и в MaterializedView витрины CDC тоже)
                token = f"telemetry:{window['low']}:{window['high']}:{batch_no}"
                ch.insert("telemetry_raw", rows, column_names=TELEMETRY_COLUMNS,
                          settings={"insert_deduplication_token": token,
                                    "deduplicate_blocks_in_dependent_materialized_views": 1})
                total += len(rows)
                batch_no += 1
    log.info("telemetry rows loaded: %d", total)
    return total


def build_report_mart(ti=None, **_) -> None:
    if crm_source() == "cdc":
        log.info("crm_source=cdc: report mart is maintained by MaterializedView reports.mv_report_daily_cdc")
        return
    window = ti.xcom_pull(task_ids="extract_window")
    ch = ch_client()
    # Дни, в которые попали события из загруженного интервала (включая опоздавшие)
    days = ch.query(
        """SELECT DISTINCT toDate(ts) FROM reports.telemetry_raw
           WHERE received_at > parseDateTime64BestEffort({low:String}, 3)
             AND received_at <= parseDateTime64BestEffort({high:String}, 3)""",
        parameters=window,
    ).result_rows
    days = [d[0] for d in days]
    if not days:
        log.info("no new telemetry, mart is up to date")
        return
    # Полный пересчет затронутых дней: ReplacingMergeTree(built_at) оставит новую версию строк
    ch.command(
        """
        INSERT INTO reports.report_daily
        SELECT
            c.email                              AS customer_email,
            c.customer_id                        AS customer_id,
            c.full_name                          AS full_name,
            c.country                            AS country,
            t.prosthesis_serial                  AS prosthesis_serial,
            p.model                              AS prosthesis_model,
            toDate(t.ts)                         AS report_date,
            count()                              AS events_count,
            countIf(t.movement_type != 'idle')   AS movements_count,
            countIf(t.error_code != 0)           AS errors_count,
            countIf(t.reaction_ms > 100)         AS slow_reactions_count,
            avg(t.reaction_ms)                   AS avg_reaction_ms,
            quantile(0.95)(t.reaction_ms)        AS p95_reaction_ms,
            max(t.reaction_ms)                   AS max_reaction_ms,
            avg(t.signal_amplitude)              AS avg_signal_amplitude,
            min(t.battery_level)                 AS min_battery_level,
            avg(t.battery_level)                 AS avg_battery_level,
            now()                                AS built_at
        FROM reports.telemetry_raw AS t FINAL
        INNER JOIN (SELECT * FROM reports.crm_prostheses FINAL) AS p ON p.serial_number = t.prosthesis_serial
        INNER JOIN (SELECT * FROM reports.crm_customers FINAL) AS c ON c.customer_id = p.customer_id
        WHERE toDate(t.ts) IN {days:Array(Date)}
        GROUP BY customer_email, customer_id, full_name, country,
                 prosthesis_serial, prosthesis_model, report_date
        """,
        parameters={"days": days},
    )
    log.info("report mart rebuilt for %d days: %s..%s", len(days), min(days), max(days))


def commit_watermark(ti=None, run_id=None, **_) -> None:
    window = ti.xcom_pull(task_ids="extract_window")
    rows = ti.xcom_pull(task_ids="load_telemetry") or 0
    ch_client().insert(
        "etl_watermark",
        [[run_id, datetime.fromisoformat(window["low"]), datetime.fromisoformat(window["high"]), rows]],
        column_names=["dag_run_id", "processed_from", "processed_until", "rows_loaded"],
    )
    log.info("watermark committed: %s", window["high"])


default_args = {
    "owner": "bionicpro-data",
    "retries": 2,
    "retry_delay": timedelta(minutes=1),
}

with DAG(
    dag_id="bionicpro_reports_etl",
    description="Telemetry + CRM -> ClickHouse, report mart for reports-api",
    default_args=default_args,
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    schedule="0 * * * *",          # каждый час
    catchup=False,
    max_active_runs=1,             # прогоны строго последовательно (watermark)
    tags=["bionicpro", "reports", "clickhouse"],
) as dag:
    t_window = PythonOperator(task_id="extract_window", python_callable=extract_window)
    t_crm = PythonOperator(task_id="load_crm", python_callable=load_crm)
    t_telemetry = PythonOperator(task_id="load_telemetry", python_callable=load_telemetry)
    t_mart = PythonOperator(task_id="build_report_mart", python_callable=build_report_mart)
    t_commit = PythonOperator(task_id="commit_watermark", python_callable=commit_watermark)

    t_window >> [t_crm, t_telemetry] >> t_mart >> t_commit
