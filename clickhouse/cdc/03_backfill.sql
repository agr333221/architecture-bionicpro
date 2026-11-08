-- Разовое наполнение витрины CDC телеметрией, загруженной до включения MaterializedView.
-- Выполняется один раз (идемпотентность обеспечивает проверка на пустую витрину).
INSERT INTO reports.report_daily_cdc
SELECT
    p.customer_id,
    toDate(t.ts),
    t.prosthesis_serial,
    count(),
    countIf(t.movement_type != 'idle'),
    countIf(t.error_code != 0),
    countIf(t.reaction_ms > 100),
    avgState(t.reaction_ms),
    quantileState(0.95)(t.reaction_ms),
    max(t.reaction_ms),
    avgState(t.signal_amplitude),
    min(t.battery_level),
    avgState(t.battery_level)
FROM reports.telemetry_raw AS t FINAL
INNER JOIN
(
    SELECT serial_number, customer_id FROM reports.crm_prostheses_cdc FINAL WHERE is_deleted = 0
) AS p ON p.serial_number = t.prosthesis_serial
WHERE (SELECT count() FROM reports.report_daily_cdc) = 0
GROUP BY p.customer_id, toDate(t.ts), t.prosthesis_serial;
