-- VOICE AI - MULTI-TENANT DATABASE SCHEMA
-- =========================================

-- Firmy (tenants)
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone_number TEXT UNIQUE NOT NULL,  -- numer Twilio przypisany do firmy
    address TEXT,
    email TEXT,
    google_calendar_id TEXT,            -- do integracji z kalendarzem
    timezone TEXT DEFAULT 'Europe/Warsaw',
    greeting_text TEXT,                 -- opcjonalne własne powitanie
    goodbye_text TEXT,                  -- opcjonalne własne pożegnanie
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Godziny otwarcia
CREATE TABLE IF NOT EXISTS working_hours (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    day_of_week INTEGER NOT NULL,       -- 0=poniedziałek, 6=niedziela
    open_time TEXT,                     -- format HH:MM, NULL = zamknięte
    close_time TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

-- Usługi
CREATE TABLE IF NOT EXISTS services (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,  -- czas trwania w minutach
    price REAL NOT NULL,
    currency TEXT DEFAULT 'PLN',
    description TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

-- Pracownicy
CREATE TABLE IF NOT EXISTS staff (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT,                          -- np. "fryzjer", "barber", "trener"
    phone TEXT,
    email TEXT,
    google_calendar_id TEXT,            -- własny kalendarz pracownika
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

-- Rezerwacje
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    service_id TEXT NOT NULL,
    staff_id TEXT,                      -- opcjonalnie konkretny pracownik
    customer_name TEXT,
    customer_phone TEXT NOT NULL,
    booking_date TEXT NOT NULL,         -- format YYYY-MM-DD
    booking_time TEXT NOT NULL,         -- format HH:MM
    duration_minutes INTEGER NOT NULL,
    status TEXT DEFAULT 'confirmed',    -- confirmed, cancelled, completed
    notes TEXT,
    call_sid TEXT,                      -- ID rozmowy Twilio
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (service_id) REFERENCES services(id),
    FOREIGN KEY (staff_id) REFERENCES staff(id)
);

-- Logi rozmów (do analizy i debugowania)
CREATE TABLE IF NOT EXISTS call_logs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    call_sid TEXT NOT NULL,             -- Twilio Call SID
    caller_phone TEXT,
    started_at TEXT,
    ended_at TEXT,
    duration_seconds INTEGER,
    transcript TEXT,                    -- pełna transkrypcja
    intents_log TEXT,                   -- JSON z intentami
    booking_id TEXT,                    -- jeśli utworzono rezerwację
    status TEXT DEFAULT 'completed',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (booking_id) REFERENCES bookings(id)
);

-- Indeksy dla szybkiego wyszukiwania
CREATE INDEX IF NOT EXISTS idx_tenants_phone ON tenants(phone_number);
CREATE INDEX IF NOT EXISTS idx_working_hours_tenant ON working_hours(tenant_id);
CREATE INDEX IF NOT EXISTS idx_services_tenant ON services(tenant_id);
CREATE INDEX IF NOT EXISTS idx_staff_tenant ON staff(tenant_id);
CREATE INDEX IF NOT EXISTS idx_bookings_tenant_date ON bookings(tenant_id, booking_date);
CREATE INDEX IF NOT EXISTS idx_call_logs_tenant ON call_logs(tenant_id);

-- =========================================
-- PRZYKŁADOWE DANE (Salon Fryzjerski Anna)
-- =========================================

-- Firma
INSERT OR IGNORE INTO tenants (id, name, phone_number, address, email, timezone)
VALUES (
    'tenant_001',
    'Salon Fryzjerski Anna',
    '+48732071272',
    'ul. Kwiatowa 15, Warszawa',
    'kontakt@salonanna.pl',
    'Europe/Warsaw'
);

-- Godziny otwarcia (pon-pt 9-17, czw 9-19, sob 10-14, nd zamknięte)
INSERT OR IGNORE INTO working_hours (id, tenant_id, day_of_week, open_time, close_time) VALUES
('wh_001_0', 'tenant_001', 0, '09:00', '17:00'),  -- poniedziałek
('wh_001_1', 'tenant_001', 1, '09:00', '17:00'),  -- wtorek
('wh_001_2', 'tenant_001', 2, '09:00', '17:00'),  -- środa
('wh_001_3', 'tenant_001', 3, '09:00', '19:00'),  -- czwartek (dłużej)
('wh_001_4', 'tenant_001', 4, '09:00', '17:00'),  -- piątek
('wh_001_5', 'tenant_001', 5, '10:00', '14:00'),  -- sobota
('wh_001_6', 'tenant_001', 6, NULL, NULL);        -- niedziela (zamknięte)

-- Usługi
INSERT OR IGNORE INTO services (id, tenant_id, name, duration_minutes, price) VALUES
('svc_001_1', 'tenant_001', 'Strzyżenie damskie', 60, 80),
('svc_001_2', 'tenant_001', 'Strzyżenie męskie', 30, 50),
('svc_001_3', 'tenant_001', 'Koloryzacja', 120, 200),
('svc_001_4', 'tenant_001', 'Modelowanie', 45, 60),
('svc_001_5', 'tenant_001', 'Strzyżenie + modelowanie', 75, 120);

-- Pracownicy
INSERT OR IGNORE INTO staff (id, tenant_id, name, role) VALUES
('staff_001_1', 'tenant_001', 'Anna Kowalska', 'fryzjer'),
('staff_001_2', 'tenant_001', 'Maria Nowak', 'fryzjer'),
('staff_001_3', 'tenant_001', 'Katarzyna Wiśniewska', 'fryzjer');