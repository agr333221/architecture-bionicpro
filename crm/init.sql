-- CRM BionicPRO (OLTP, PostgreSQL). Упрощенная модель: клиенты и их протезы.
-- wal_level=logical включен в docker-compose: таблицы захватываются Debezium (задание 4).

CREATE TABLE customers (
    id          SERIAL PRIMARY KEY,
    email       VARCHAR(255) NOT NULL UNIQUE,
    full_name   VARCHAR(255) NOT NULL,
    country     VARCHAR(2)   NOT NULL DEFAULT 'RU',
    phone       VARCHAR(32),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE prostheses (
    id            SERIAL PRIMARY KEY,
    customer_id   INTEGER      NOT NULL REFERENCES customers(id),
    serial_number VARCHAR(32)  NOT NULL UNIQUE,
    model         VARCHAR(64)  NOT NULL,
    side          VARCHAR(8)   NOT NULL,
    status        VARCHAR(16)  NOT NULL DEFAULT 'active',
    installed_at  TIMESTAMPTZ  NOT NULL,
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX prostheses_customer_idx ON prostheses(customer_id);

-- Полный before-image для событий UPDATE/DELETE в Debezium
ALTER TABLE customers  REPLICA IDENTITY FULL;
ALTER TABLE prostheses REPLICA IDENTITY FULL;

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER customers_touch  BEFORE UPDATE ON customers  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER prostheses_touch BEFORE UPDATE ON prostheses FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Тестовые клиенты. email совпадает с email пользователей Keycloak/LDAP,
-- по нему сервис отчетов связывает пользователя с клиентом CRM.
INSERT INTO customers (email, full_name, country, phone) VALUES
    ('prothetic1@example.com', 'Prothetic One',   'RU', '+7 900 000-00-01'),
    ('prothetic2@example.com', 'Prothetic Two',   'RU', '+7 900 000-00-02'),
    ('prothetic3@example.com', 'Prothetic Three', 'RU', '+7 900 000-00-03'),
    ('john@example.com',       'John Doe',        'DE', '+49 30 0000001'),
    ('alex@example.com',       'Alex Johnson',    'DE', '+49 30 0000002'),
    ('maria@example.com',      'Maria Petrova',   'RU', NULL),
    ('ivan@example.com',       'Ivan Sidorov',    'RU', NULL);

INSERT INTO prostheses (customer_id, serial_number, model, side, installed_at) VALUES
    (1, 'BP-ARM-0001', 'BionicPRO Arm X2',  'left',  now() - interval '200 days'),
    (1, 'BP-ARM-0002', 'BionicPRO Arm X2',  'right', now() - interval '120 days'),
    (2, 'BP-ARM-0003', 'BionicPRO Arm X1',  'left',  now() - interval '300 days'),
    (3, 'BP-HND-0004', 'BionicPRO Hand H1', 'right', now() - interval '90 days'),
    (4, 'BP-ARM-0005', 'BionicPRO Arm X2',  'right', now() - interval '150 days'),
    (5, 'BP-HND-0006', 'BionicPRO Hand H1', 'left',  now() - interval '60 days'),
    (6, 'BP-ARM-0007', 'BionicPRO Arm X1',  'left',  now() - interval '400 days'),
    (7, 'BP-HND-0008', 'BionicPRO Hand H1', 'right', now() - interval '30 days');
