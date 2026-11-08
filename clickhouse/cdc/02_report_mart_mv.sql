-- Задание 4. Витрина отчетности на MaterializedView.
-- Телеметрию по-прежнему загружает Airflow в reports.telemetry_raw,
-- а данные CRM приходят через CDC (Debezium -> Kafka -> KafkaEngine).
-- При каждой вставке в telemetry_raw MaterializedView агрегирует новый блок,
-- объединяет его с актуальными данными CRM (протез -> клиент) и дописывает
-- промежуточные состояния агрегатов в AggregatingMergeTree.
-- Сервис отчетов читает витрину по ключу (customer_id, report_date) и
-- лишь финализирует агрегаты (-Merge), без тяжелых вычислений.

CREATE TABLE IF NOT EXISTS reports.report_daily_cdc
(
    customer_id          UInt32,
    report_date          Date,
    prosthesis_serial    String,
    events_count         SimpleAggregateFunction(sum, UInt64),
    movements_count      SimpleAggregateFunction(sum, UInt64),
    errors_count         SimpleAggregateFunction(sum, UInt64),
    slow_reactions_count SimpleAggregateFunction(sum, UInt64),
    avg_reaction_ms      AggregateFunction(avg, UInt16),
    p95_reaction_ms      AggregateFunction(quantile(0.95), UInt16),
    max_reaction_ms      SimpleAggregateFunction(max, UInt16),
    avg_signal_amplitude AggregateFunction(avg, Float32),
    min_battery_level    SimpleAggregateFunction(min, UInt8),
    avg_battery_level    AggregateFunction(avg, UInt8)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(report_date)
ORDER BY (customer_id, report_date, prosthesis_serial);

CREATE MATERIALIZED VIEW IF NOT EXISTS reports.mv_report_daily_cdc
TO reports.report_daily_cdc AS
SELECT
    p.customer_id                         AS customer_id,
    toDate(t.ts)                          AS report_date,
    t.prosthesis_serial                   AS prosthesis_serial,
    count()                               AS events_count,
    countIf(t.movement_type != 'idle')    AS movements_count,
    countIf(t.error_code != 0)            AS errors_count,
    countIf(t.reaction_ms > 100)          AS slow_reactions_count,
    avgState(t.reaction_ms)               AS avg_reaction_ms,
    quantileState(0.95)(t.reaction_ms)    AS p95_reaction_ms,
    max(t.reaction_ms)                    AS max_reaction_ms,
    avgState(t.signal_amplitude)          AS avg_signal_amplitude,
    min(t.battery_level)                  AS min_battery_level,
    avgState(t.battery_level)             AS avg_battery_level
FROM reports.telemetry_raw AS t
INNER JOIN
(
    SELECT serial_number, customer_id
    FROM reports.crm_prostheses_cdc FINAL
    WHERE is_deleted = 0
) AS p ON p.serial_number = t.prosthesis_serial
GROUP BY customer_id, report_date, prosthesis_serial;
