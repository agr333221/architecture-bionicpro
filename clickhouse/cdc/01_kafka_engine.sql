-- Задание 4. Прием изменений CRM из Kafka (Debezium) через KafkaEngine.
-- Debezium отправляет "плоские" записи (SMT ExtractNewRecordState) в JSON без схемы:
--   {"id":1,"email":"...","updated_at":"2026-11-08T10:00:00.000000Z",
--    "__op":"c","__lsn":12345,"__source_ts_ms":...,"__deleted":"false"}
-- Схема: Kafka-таблица (очередь) -> MaterializedView -> ReplacingMergeTree (актуальное состояние).

-- ---------- customers ----------
CREATE TABLE IF NOT EXISTS reports.kafka_crm_customers
(
    id         UInt32,
    email      String,
    full_name  String,
    country    String,
    updated_at String,
    __op       String,
    __lsn      UInt64,
    __deleted  String
)
ENGINE = Kafka
SETTINGS
    kafka_broker_list = 'kafka:9092',
    kafka_topic_list = 'crm.public.customers',
    kafka_group_name = 'clickhouse-crm-customers',
    kafka_format = 'JSONEachRow',
    kafka_num_consumers = 1,
    kafka_skip_broken_messages = 100,
    input_format_skip_unknown_fields = 1;

CREATE TABLE IF NOT EXISTS reports.crm_customers_cdc
(
    customer_id UInt32,
    email       String,
    full_name   String,
    country     LowCardinality(String),
    updated_at  DateTime64(6, 'UTC'),
    _version    UInt64,
    is_deleted  UInt8,
    ingested_at DateTime('UTC') DEFAULT now()
)
ENGINE = ReplacingMergeTree(_version, is_deleted)
ORDER BY customer_id;

CREATE MATERIALIZED VIEW IF NOT EXISTS reports.mv_crm_customers_cdc
TO reports.crm_customers_cdc AS
SELECT
    id                                               AS customer_id,
    lower(email)                                     AS email,
    full_name,
    country,
    parseDateTime64BestEffortOrZero(updated_at, 6)   AS updated_at,
    __lsn                                            AS _version,
    toUInt8(__deleted = 'true' OR __op = 'd')        AS is_deleted
FROM reports.kafka_crm_customers;

-- ---------- prostheses ----------
CREATE TABLE IF NOT EXISTS reports.kafka_crm_prostheses
(
    id            UInt32,
    customer_id   UInt32,
    serial_number String,
    model         String,
    side          String,
    status        String,
    installed_at  String,
    updated_at    String,
    __op          String,
    __lsn         UInt64,
    __deleted     String
)
ENGINE = Kafka
SETTINGS
    kafka_broker_list = 'kafka:9092',
    kafka_topic_list = 'crm.public.prostheses',
    kafka_group_name = 'clickhouse-crm-prostheses',
    kafka_format = 'JSONEachRow',
    kafka_num_consumers = 1,
    kafka_skip_broken_messages = 100,
    input_format_skip_unknown_fields = 1;

CREATE TABLE IF NOT EXISTS reports.crm_prostheses_cdc
(
    prosthesis_id UInt32,
    customer_id   UInt32,
    serial_number String,
    model         LowCardinality(String),
    side          LowCardinality(String),
    status        LowCardinality(String),
    installed_at  DateTime64(6, 'UTC'),
    updated_at    DateTime64(6, 'UTC'),
    _version      UInt64,
    is_deleted    UInt8,
    ingested_at   DateTime('UTC') DEFAULT now()
)
ENGINE = ReplacingMergeTree(_version, is_deleted)
ORDER BY prosthesis_id;

CREATE MATERIALIZED VIEW IF NOT EXISTS reports.mv_crm_prostheses_cdc
TO reports.crm_prostheses_cdc AS
SELECT
    id                                                 AS prosthesis_id,
    customer_id,
    serial_number,
    model,
    side,
    status,
    parseDateTime64BestEffortOrZero(installed_at, 6)   AS installed_at,
    parseDateTime64BestEffortOrZero(updated_at, 6)     AS updated_at,
    __lsn                                              AS _version,
    toUInt8(__deleted = 'true' OR __op = 'd')          AS is_deleted
FROM reports.kafka_crm_prostheses;
