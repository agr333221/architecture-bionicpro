-- OLAP-хранилище BionicPRO (ClickHouse). Слои:
--   raw     : telemetry_raw, crm_customers, crm_prostheses (загружает Airflow)
--   mart    : report_daily - витрина отчетов по клиентам (строит Airflow)
--   service : etl_watermark - граница обработанных ETL данных

CREATE DATABASE IF NOT EXISTS reports;

-- Сырая телеметрия. Партиции по месяцам, сортировка по протезу и времени.
-- non_replicated_deduplication_window: повторная вставка того же батча (retry задачи Airflow)
-- с тем же insert_deduplication_token игнорируется.
CREATE TABLE IF NOT EXISTS reports.telemetry_raw
(
    event_id          UInt64,
    prosthesis_serial LowCardinality(String),
    ts                DateTime64(3, 'UTC'),
    received_at       DateTime64(3, 'UTC'),
    movement_type     LowCardinality(String),
    signal_amplitude  Float32,
    reaction_ms       UInt16,
    battery_level     UInt8,
    error_code        UInt16,
    loaded_at         DateTime('UTC') DEFAULT now()
)
ENGINE = ReplacingMergeTree(loaded_at)
PARTITION BY toYYYYMM(ts)
ORDER BY (prosthesis_serial, ts, event_id)
SETTINGS non_replicated_deduplication_window = 1000;

-- Снимки CRM (Airflow, задание 2)
CREATE TABLE IF NOT EXISTS reports.crm_customers
(
    customer_id UInt32,
    email       String,
    full_name   String,
    country     LowCardinality(String),
    updated_at  DateTime64(6, 'UTC'),
    loaded_at   DateTime('UTC') DEFAULT now()
)
ENGINE = ReplacingMergeTree(loaded_at)
ORDER BY customer_id;

CREATE TABLE IF NOT EXISTS reports.crm_prostheses
(
    prosthesis_id UInt32,
    customer_id   UInt32,
    serial_number String,
    model         LowCardinality(String),
    side          LowCardinality(String),
    status        LowCardinality(String),
    installed_at  DateTime64(6, 'UTC'),
    updated_at    DateTime64(6, 'UTC'),
    loaded_at     DateTime('UTC') DEFAULT now()
)
ENGINE = ReplacingMergeTree(loaded_at)
ORDER BY prosthesis_id;

-- Витрина отчетности (Airflow, задание 2).
-- Одна строка = клиент x день x протез, все метрики уже посчитаны.
-- ORDER BY начинается с email клиента: отчет пользователя читается
-- одним диапазоном первичного ключа (быстрый доступ по пользователю).
CREATE TABLE IF NOT EXISTS reports.report_daily
(
    customer_email       String,
    customer_id          UInt32,
    full_name            String,
    country              LowCardinality(String),
    prosthesis_serial    String,
    prosthesis_model     LowCardinality(String),
    report_date          Date,
    events_count         UInt32,
    movements_count      UInt32,
    errors_count         UInt32,
    slow_reactions_count UInt32,
    avg_reaction_ms      Float32,
    p95_reaction_ms      Float32,
    max_reaction_ms      UInt16,
    avg_signal_amplitude Float32,
    min_battery_level    UInt8,
    avg_battery_level    Float32,
    built_at             DateTime('UTC') DEFAULT now()
)
ENGINE = ReplacingMergeTree(built_at)
PARTITION BY toYYYYMM(report_date)
ORDER BY (customer_email, report_date, prosthesis_serial);

-- Watermark: до какого момента (по времени поступления данных) ETL все обработал
CREATE TABLE IF NOT EXISTS reports.etl_watermark
(
    dag_run_id      String,
    processed_from  DateTime('UTC'),
    processed_until DateTime('UTC'),
    rows_loaded     UInt64,
    finished_at     DateTime('UTC') DEFAULT now()
)
ENGINE = MergeTree
ORDER BY processed_until;
