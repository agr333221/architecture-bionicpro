-- БД телеметрии протезов (данные с датчиков, приходят через 4G-модуль).
CREATE TABLE telemetry_events (
    id                BIGSERIAL PRIMARY KEY,
    prosthesis_serial VARCHAR(32)  NOT NULL,
    ts                TIMESTAMPTZ  NOT NULL,              -- время события на протезе
    received_at       TIMESTAMPTZ  NOT NULL DEFAULT now(), -- время поступления на сервер
    movement_type     VARCHAR(16)  NOT NULL,
    signal_amplitude  REAL         NOT NULL,              -- амплитуда миосигнала, мВ
    reaction_ms       SMALLINT     NOT NULL,              -- время реакции протеза
    battery_level     SMALLINT     NOT NULL,              -- заряд батареи, %
    error_code        SMALLINT     NOT NULL DEFAULT 0
);
-- Инкрементальная выгрузка в DWH идет по времени поступления
CREATE INDEX telemetry_received_idx ON telemetry_events(received_at);

-- Тестовые данные: 45 дней, событие каждые 10 минут по каждому протезу
INSERT INTO telemetry_events (prosthesis_serial, ts, received_at, movement_type,
                              signal_amplitude, reaction_ms, battery_level, error_code)
SELECT
    s.serial,
    g.ts,
    g.ts + (random() * 120) * interval '1 second',
    (ARRAY['grip','pinch','wrist_rotate','release','point','idle'])[1 + floor(random() * 6)::int],
    round((0.2 + random() * 1.3)::numeric, 3),
    greatest(30, (s.base_reaction + (random() * 40 - 15) + case when random() < 0.05 then 60 else 0 end))::smallint,
    (100 - (extract(epoch from g.ts)::bigint / 600 % 144) * 0.6)::smallint,
    case when random() < 0.01 then (1 + floor(random() * 5))::smallint else 0 end
FROM (VALUES ('BP-ARM-0001', 70), ('BP-ARM-0002', 65), ('BP-ARM-0003', 85),
             ('BP-HND-0004', 60), ('BP-ARM-0005', 75), ('BP-HND-0006', 90),
             ('BP-ARM-0007', 80), ('BP-HND-0008', 55)) AS s(serial, base_reaction)
CROSS JOIN generate_series(date_trunc('day', now()) - interval '45 days',
                           now() - interval '5 minutes',
                           interval '10 minutes') AS g(ts);
