# Factory IoT System — Project Reference

> **v2.0 — July 2026**
> Interface: RS485 Modbus RTU (field) · RJ45 Ethernet (MQTT transport)
> Critical fixes applied: compound identity, boot_id sequence scoping, ISO-compliant OEE/MTBF

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Compound Identity Design](#2-compound-identity-design)
3. [RS485 Interface Spec](#3-rs485-interface-spec)
4. [RJ45 / MQTT Interface Spec](#4-rj45--mqtt-interface-spec)
5. [Controller Identity Store](#5-controller-identity-store)
6. [SQLite Schema — Edge Controller](#6-sqlite-schema--edge-controller)
7. [Modbus Polling Loop](#7-modbus-polling-loop)
8. [Sync Agent](#8-sync-agent)
9. [Server: Event Ingest & Gap Detection](#9-server-event-ingest--gap-detection)
10. [TimescaleDB Schema](#10-timescaledb-schema)
11. [Maintenance Request Module](#11-maintenance-request-module)
12. [Reliability Metrics (ISO-Compliant)](#12-reliability-metrics-iso-compliant)
13. [Telegram & AI Layer](#13-telegram--ai-layer)
14. [Implementation Checklist](#14-implementation-checklist)
15. [Critical Rules — Do Not Break](#15-critical-rules--do-not-break)

---

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  ① FIELD / HARDWARE                                             │
│  PLC · Sensors · RFID · Remote I/O Module (button + LED)       │
│  All speak Modbus RTU as slaves                                  │
└───────────────────────┬─────────────────────────────────────────┘
                        │ RS485 bus · 120Ω terminated · ≤32 slaves
┌───────────────────────▼─────────────────────────────────────────┐
│  ② EDGE / Pi CONTROLLER                                         │
│  RS485 HAT → Modbus master poll (100 ms)                        │
│  /etc/ctrl_id.json  (plant · line · zone · station · boot_id)  │
│  SQLite event_log   (append-only, all 6 identity fields)        │
│  Sync Agent         (QoS 1 publish, ACK-only synced flag)       │
└───────────────────────┬─────────────────────────────────────────┘
                        │ RJ45 Ethernet · MQTT QoS 1 · TLS
┌───────────────────────▼─────────────────────────────────────────┐
│  ③ TRANSPORT — EMQX MQTT Broker                                 │
│  Topic: factory/{plant}/{line}/{zone}/{station}/events/{type}   │
│  Backfill channel scoped per (station_id, boot_id)              │
└───────────────────────┬─────────────────────────────────────────┘
                        │ Subscribe → event handler
┌───────────────────────▼─────────────────────────────────────────┐
│  ④ SERVER / BACKEND                                             │
│  Python AsyncIO event handler                                   │
│  TimescaleDB: events table (6-field UNIQUE key)                 │
│  PostgreSQL: maintenance_tickets (computed downtime columns)     │
│  APScheduler: escalation check every 60 s                       │
└──────────────┬────────────────────────┬────────────────────────-┘
               │                        │
┌──────────────▼──────────┐  ┌──────────▼─────────────────────────┐
│  ⑤ ANALYTICS            │  │  ⑥ NOTIFICATION / AI               │
│  TimescaleDB queries     │  │  Telegram Bot (alerts + ACK)       │
│  ISO 22400 OEE           │  │  Claude AI (NL metric queries)     │
│  ISO 14224 MTBF/MTTF    │  │  Supervisor escalation             │
│  Grafana dashboards      │  └────────────────────────────────────┘
└─────────────────────────┘
```

---

## 2. Compound Identity Design

Every event — sensor reading, fault signal, maintenance request — carries all six fields. This is the single most important architectural decision in the system.

```
( plant_id · line_id · zone_id · station_id · boot_id · controller_seq )
```

### Why boot_id exists

**The failure mode without it:**
SQLite `AUTOINCREMENT` resets to 1 after an SD card wipe or table recreation. The server's `UNIQUE(station_id, controller_seq)` key already holds seq 1–50,000 for that station. Every new event from the reflashed controller silently collides and is discarded by `ON CONFLICT DO NOTHING`. No gap is detected (new seq < `last_known_seq`; gap detection only fires on forward jumps). Data is permanently lost in exactly the scenario where Pis fail most — SD card wear is the #1 Pi failure mode in a factory.

**The fix:**
`boot_id` is a UUID generated once at provisioning and stored in `/etc/ctrl_id.json` — **outside the SQLite event_log table**. Table recreation does not change it. A new UUID is written only on a deliberate factory reflash. The server keys on all six fields, so a post-reflash seq 1 lands in a different key space from the pre-reflash seq 1.

---

## 3. RS485 Interface Spec

| Parameter | Value | Notes |
|-----------|-------|-------|
| Protocol | Modbus RTU | Standard industrial serial |
| Baud rate | 9600–115200 bps | 19200 typical; match slave setting |
| Topology | Multi-drop daisy-chain | Up to 32 slave devices per segment |
| Max distance | 1200 m | At 9600 bps with 24 AWG twisted pair |
| Termination | 120 Ω at each end | Mandatory — prevents reflections |
| Pi hardware | RS485 HAT (UART/SPI) | e.g. Waveshare RS485/CAN HAT |
| Pi role | Modbus **master** | Single master per bus segment |
| Poll interval | 100 ms cyclic | Configurable per register criticality |
| Machine fault | PLC holding register | PLC writes fault code on fault condition |
| Maint button | Remote I/O coil read | Physical button → I/O module → coil |
| LED feedback | Remote I/O coil write | Pi writes coil HIGH on button press confirm |

**Slave address map (example — adjust per site):**

| Address | Device | Registers Used |
|---------|--------|---------------|
| 1 | Remote I/O Module | Coil 0=button, Coil 1=LED |
| 2 | Machine PLC | HR 100=fault code |
| 3 | Unit counter sensor | IR 0=count |
| 4 | RFID reader | IR 0-9=card data |
| 5+ | Additional sensors | Site-specific |

---

## 4. RJ45 / MQTT Interface Spec

| Parameter | Value | Notes |
|-----------|-------|-------|
| Protocol | MQTT 3.1.1 / 5.0 | Over TCP/IP on standard LAN |
| Security | TLS 1.2+ mutual auth | X.509 certificate per controller |
| QoS | **QoS 1** (at-least-once) | Re-delivered until broker ACKs |
| Topic pattern | `factory/{plant_id}/{line_id}/{zone_id}/{station_id}/events/{type}` | |
| Broker | EMQX (self-hosted) | Supports 10,000+ concurrent connections |
| Offline behaviour | Events buffer in SQLite | Auto-resumes flush on reconnect |
| Backfill topic | `factory/.../cmd/backfill` | Server requests missing seq range |

---

## 5. Controller Identity Store

**File: `/etc/ctrl_id.json`**
Written once at provisioning. Read-only during normal operation. New `boot_id` generated only on factory reflash.

```json
{
  "plant_id":   "PLT01",
  "line_id":    "L03",
  "zone_id":    "Z02",
  "station_id": "ST07",
  "boot_id":    "f47ac10b-58cc-4372-a567-0e02b2c3d479"
}
```

```python
# controller/identity.py
import json, uuid, pathlib

ID_FILE = pathlib.Path('/etc/ctrl_id.json')

def load_identity() -> dict:
    if ID_FILE.exists():
        return json.loads(ID_FILE.read_text())
    # First boot or deliberate reflash — generate new boot_id
    ident = {
        "plant_id":   PLANT_ID,    # set from provisioning env var
        "line_id":    LINE_ID,
        "zone_id":    ZONE_ID,
        "station_id": STATION_ID,
        "boot_id":    str(uuid.uuid4()),
    }
    ID_FILE.write_text(json.dumps(ident, indent=2))
    return ident
```

> **Rule:** Never store `boot_id` inside the `event_log` table or any table that might be recreated. `/etc/ctrl_id.json` survives SQLite recreation.

---

## 6. SQLite Schema — Edge Controller

Deploy on every controller at first boot.

```sql
CREATE TABLE event_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,  -- local order key only
    plant_id     TEXT    NOT NULL,
    line_id      TEXT    NOT NULL,
    zone_id      TEXT    NOT NULL,
    station_id   TEXT    NOT NULL,
    boot_id      TEXT    NOT NULL,   -- UUID from /etc/ctrl_id.json
    event_type   TEXT    NOT NULL,   -- sensor_reading | rfid_scan | machine_fault
                                     -- maintenance_request | system
    payload      TEXT    NOT NULL,   -- JSON blob
    created_at   INTEGER NOT NULL,   -- unix epoch milliseconds (controller clock)
    synced       INTEGER DEFAULT 0   -- 0=pending, 1=confirmed by broker ACK only
);

CREATE INDEX idx_unsynced ON event_log(synced, seq);
```

**`log_event()` helper:**

```python
# controller/event_store.py
import sqlite3, json, time

IDENT = None  # set at startup: load_identity()

def log_event(event_type: str, payload: dict):
    conn = sqlite3.connect('/var/db/event_log.db')
    conn.execute(
        """INSERT INTO event_log
           (plant_id, line_id, zone_id, station_id, boot_id, event_type, payload, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            IDENT['plant_id'], IDENT['line_id'],
            IDENT['zone_id'],  IDENT['station_id'],
            IDENT['boot_id'],  event_type,
            json.dumps(payload), int(time.time() * 1000),
        )
    )
    conn.commit()
    conn.close()
```

---

## 7. Modbus Polling Loop

```python
# controller/modbus_poll.py
from pymodbus.client import ModbusSerialClient
from event_store import log_event
from identity import load_identity

IDENT          = load_identity()
MODBUS_PORT    = '/dev/ttyS0'   # RS485 HAT UART
BAUDRATE       = 19200
IO_MODULE_ADDR = 1              # Remote I/O module slave address
PLC_ADDR       = 2              # Machine PLC slave address
BUTTON_COIL    = 0              # Coil 0x00 = maintenance button input
LED_COIL       = 1              # Coil 0x01 = indicator LED output
FAULT_REGISTER = 100            # Holding register = machine fault code
DEBOUNCE_COUNT = 3              # 3 reads × 100 ms = 300 ms debounce

client = ModbusSerialClient(
    port=MODBUS_PORT, baudrate=BAUDRATE, parity='N', stopbits=1, bytesize=8
)

prev_button = 0; btn_count = 0; prev_fault = 0

def poll_cycle():
    global prev_button, btn_count, prev_fault

    # ── Maintenance button (Remote I/O coil) ─────────────────────────────────
    r = client.read_coils(BUTTON_COIL, 1, slave=IO_MODULE_ADDR)
    btn = r.bits[0] if not r.isError() else 0

    if btn and not prev_button:
        btn_count += 1
        if btn_count >= DEBOUNCE_COUNT:
            log_event('maintenance_request', {
                **IDENT,
                'fault_seq':    get_last_fault_seq(),  # link if within 10 min
                'request_type': 'machine_down',
            })
            # Light the LED coil on the I/O module
            client.write_coil(LED_COIL, True, slave=IO_MODULE_ADDR)
            btn_count = 0
    else:
        btn_count = 0
    prev_button = btn

    # ── Machine fault register (PLC) ─────────────────────────────────────────
    r2 = client.read_holding_registers(FAULT_REGISTER, 1, slave=PLC_ADDR)
    fault = r2.registers[0] if not r2.isError() else 0

    if fault != 0 and prev_fault == 0:
        log_event('machine_fault', {
            **IDENT,
            'fault_source': 'rs485_plc_register',
            'fault_code':   fault,
        })
    prev_fault = fault

# Run in a thread at 100 ms interval
import threading, time

def start_poll():
    client.connect()
    while True:
        poll_cycle()
        time.sleep(0.1)

threading.Thread(target=start_poll, daemon=True).start()
```

> **Note:** If the LAN is down when the button is pressed, `log_event()` still succeeds — the request is stored in SQLite and synced on reconnect. The LED coil is written immediately over RS485 regardless of network state.

---

## 8. Sync Agent

```python
# controller/sync_agent.py
import json
import paho.mqtt.client as mqtt
from event_store import get_unsynced, mark_synced
from identity import load_identity

IDENT  = load_identity()
BROKER = 'mqtt.factory.local'
PORT   = 8883  # TLS

class SyncAgent:
    def __init__(self):
        self.pending_acks = {}   # mqtt_mid -> controller_seq
        self.client = mqtt.Client()
        self.client.tls_set(
            ca_certs='/etc/certs/ca.crt',
            certfile='/etc/certs/client.crt',
            keyfile='/etc/certs/client.key',
        )
        self.client.on_publish = self._on_publish
        self.client.on_connect = self._on_connect

    def _on_publish(self, client, userdata, mid):
        seq = self.pending_acks.pop(mid, None)
        if seq:
            mark_synced(seq)   # Only after broker ACK — never speculative

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._flush_pending()

    def _flush_pending(self):
        rows = get_unsynced(limit=50)
        for seq, event_type, payload_str, created_at in rows:
            msg = json.dumps({
                **IDENT,           # plant_id, line_id, zone_id, station_id, boot_id
                'seq':        seq,
                'event_type': event_type,
                'payload':    json.loads(payload_str),
                'ts':         created_at,
            })
            topic = (
                f'factory/{IDENT["plant_id"]}/{IDENT["line_id"]}/'
                f'{IDENT["zone_id"]}/{IDENT["station_id"]}/events/{event_type}'
            )
            result = self.client.publish(topic, msg, qos=1)
            self.pending_acks[result.mid] = seq

    def run(self):
        self.client.connect(BROKER, PORT)
        self.client.loop_forever()
```

---

## 9. Server: Event Ingest & Gap Detection

Gap detection is scoped per `(station_id, boot_id)`. A new `boot_id` means a reboot — not a data gap.

```python
# server/event_handler.py

async def handle_incoming_event(pool, raw: dict):
    station_id = raw['station_id']
    boot_id    = raw['boot_id']
    seq        = raw['seq']

    last_seq = await get_last_known_seq(pool, station_id, boot_id)

    if last_seq is None:
        # New boot_id — first connect or reboot after reflash
        await log_reboot_event(pool, station_id, boot_id)

    elif seq > last_seq + 1:
        # Forward gap within same boot — request backfill
        await request_backfill(station_id, boot_id, last_seq + 1, seq - 1)

    elif seq <= last_seq:
        # seq went backward — anomaly (do NOT discard, do log)
        await log_anomaly(pool, station_id, boot_id, seq, last_seq)

    await pool.execute('''
        INSERT INTO events
            (plant_id, line_id, zone_id, station_id, boot_id,
             controller_seq, event_type, payload, controller_ts)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8, to_timestamp($9/1000.0))
        ON CONFLICT (plant_id, line_id, zone_id, station_id, boot_id, controller_seq)
        DO NOTHING
    ''',
        raw['plant_id'], raw['line_id'], raw['zone_id'],
        station_id, boot_id, seq,
        raw['event_type'], json.dumps(raw['payload']), raw['ts']
    )
```

**Last-known-seq table:**

```sql
CREATE TABLE station_seq_state (
    station_id   TEXT NOT NULL,
    boot_id      TEXT NOT NULL,
    last_seq     BIGINT NOT NULL,
    updated_at   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (station_id, boot_id)
);
```

---

## 10. TimescaleDB Schema

### events table

```sql
CREATE TABLE events (
    id              BIGSERIAL,
    plant_id        TEXT         NOT NULL,
    line_id         TEXT         NOT NULL,
    zone_id         TEXT         NOT NULL,
    station_id      TEXT         NOT NULL,
    boot_id         TEXT         NOT NULL,
    controller_seq  BIGINT       NOT NULL,
    event_type      TEXT         NOT NULL,
    payload         JSONB        NOT NULL,
    controller_ts   TIMESTAMPTZ  NOT NULL,   -- ALWAYS USE THIS for metrics
    server_ts       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (plant_id, line_id, zone_id, station_id, boot_id, controller_seq)
);

SELECT create_hypertable('events', 'controller_ts');
CREATE INDEX ON events (station_id, boot_id, controller_seq);
CREATE INDEX ON events (station_id, event_type, controller_ts DESC);
```

> ⚠️ **Always use `controller_ts`, never `server_ts`** for OEE, MTBF, and all production metrics. `server_ts` reflects network latency and outage gaps — not actual production time.

### maintenance_tickets table

```sql
CREATE TABLE maintenance_tickets (
    id                  BIGSERIAL PRIMARY KEY,
    plant_id            TEXT        NOT NULL,
    line_id             TEXT        NOT NULL,
    zone_id             TEXT        NOT NULL,
    station_id          TEXT        NOT NULL,
    machine_id          TEXT        NOT NULL,
    fault_boot_id       TEXT,
    fault_event_seq     BIGINT,                -- FK to events (nullable)
    request_boot_id     TEXT        NOT NULL,
    request_event_seq   BIGINT      NOT NULL,  -- FK to events
    fault_detected_at   TIMESTAMPTZ,           -- from PLC holding register poll
    request_raised_at   TIMESTAMPTZ NOT NULL,  -- from Remote I/O coil read
    engineer_id         TEXT,
    engineer_ack_at     TIMESTAMPTZ,
    repair_completed_at TIMESTAMPTZ,
    status              TEXT        NOT NULL DEFAULT 'RAISED',
    root_cause          TEXT,
    escalated           BOOLEAN     DEFAULT false,
    downtime_minutes    NUMERIC GENERATED ALWAYS AS (
        EXTRACT(EPOCH FROM (repair_completed_at - fault_detected_at)) / 60
    ) STORED,
    response_minutes    NUMERIC GENERATED ALWAYS AS (
        EXTRACT(EPOCH FROM (engineer_ack_at - request_raised_at)) / 60
    ) STORED
);
```

---

## 11. Maintenance Request Module

### Timestamp definitions

| Timestamp | Source | Used For |
|-----------|--------|----------|
| `fault_detected_at` | PLC holding register via RS485 poll | MTTR start, `downtime_minutes` |
| `request_raised_at` | Remote I/O coil read via RS485 | Engineer response time start |
| `engineer_ack_at` | Telegram inline button tap | Response time end |
| `repair_completed_at` | Engineer closes ticket | MTTR end, downtime end |

> **MTTR** = `repair_completed_at − fault_detected_at`
> **Response time** = `engineer_ack_at − request_raised_at`
> These are different metrics. Both matter.

### Ticket lifecycle

```
RAISED → ACKNOWLEDGED → IN_PROGRESS → COMPLETED
                    ↓ (if no ACK in 15 min)
                ESCALATED
```

### Telegram alert handler

```python
async def handle_maintenance_request(ticket: dict):
    msg = await bot.send_message(
        chat_id=MAINTENANCE_GROUP_ID,
        text=(
            f'🔴 *MACHINE DOWN — {ticket["machine_id"]}*\n'
            f'Plant: {ticket["plant_id"]}  Line: {ticket["line_id"]}\n'
            f'Zone: {ticket["zone_id"]}  Station: {ticket["station_id"]}\n'
            f'Reported: {ticket["request_raised_at"]}\n'
            f'Ticket: #{ticket["id"]}'
        ),
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ I am going', callback_data=f'ack:{ticket["id"]}'),
            InlineKeyboardButton('📋 Details',   callback_data=f'detail:{ticket["id"]}'),
        ]])
    )
    await store_ticket_message_id(ticket['id'], msg.message_id)


async def handle_ack(callback_query, ticket_id: int):
    engineer_id = callback_query.from_user.id
    await db.execute(
        'UPDATE maintenance_tickets SET status=$1, engineer_id=$2, engineer_ack_at=NOW() WHERE id=$3',
        'ACKNOWLEDGED', engineer_id, ticket_id
    )
    await bot.edit_message_text(
        chat_id=MAINTENANCE_GROUP_ID,
        message_id=stored_message_id,
        text=f'🟡 *ACKNOWLEDGED — {machine_id}*\nEngineer: {engineer_name}\n...'
    )
```

### Auto-escalation

```python
# Runs via APScheduler every 60 seconds
async def escalation_check(bot):
    unacked = await db.fetch('''
        SELECT * FROM maintenance_tickets
        WHERE status = 'RAISED'
          AND request_raised_at < NOW() - INTERVAL '15 minutes'
          AND escalated = false
    ''')
    for ticket in unacked:
        await bot.send_message(
            SUPERVISOR_GROUP_ID,
            f'⚠️ *ESCALATED — No response in 15 min*\n'
            f'Machine: {ticket["machine_id"]}  Ticket #{ticket["id"]}'
        )
        await db.execute(
            'UPDATE maintenance_tickets SET escalated=true WHERE id=$1',
            ticket['id']
        )
```

---

## 12. Reliability Metrics (ISO-Compliant)

### Metric definitions

| Metric | Standard | Formula |
|--------|----------|---------|
| OEE | ISO 22400-2 | Availability × Performance × Quality |
| Availability | ISO 22400-2 | (PPT − unplanned_downtime) ÷ PPT |
| Performance | ISO 22400-2 | (actual_output × ideal_cycle_time) ÷ **Actual Production Time** |
| Quality | ISO 22400-2 | good_count ÷ total_inspected |
| MTBF | ISO 14224 | Total operating time ÷ failures **(incl. trailing interval)** |
| MTBA | ISO 14224 | Same as MTBF, all assist types |
| MTTR | ISO 14224 | `repair_completed_at − fault_detected_at` |
| Response | SLA | `engineer_ack_at − request_raised_at` |
| MTTF | ISO 14224 | Avg operating time per component type until failure |

---

### OEE — Availability

```sql
SELECT
    machine_id,
    SUM(downtime_minutes)                        AS total_downtime_min,
    (planned_minutes - SUM(downtime_minutes))
        / planned_minutes                        AS availability
FROM maintenance_tickets
WHERE station_id = $1
  AND fault_detected_at BETWEEN $2 AND $3
  AND status = 'COMPLETED'
GROUP BY machine_id;
-- planned_minutes: from production_plan table, not hardcoded
```

### OEE — Performance (corrected)

> ⚠️ **The bug:** using `MAX(controller_ts) - MIN(controller_ts)` as the denominator includes downtime periods. This double-counts downtime — once in Availability and again in Performance.
> ✅ **The fix:** denominator is **Actual Production Time** = PPT − confirmed downtime.

```sql
WITH downtime_sec AS (
    SELECT COALESCE(SUM(downtime_minutes) * 60, 0) AS dt_sec
    FROM maintenance_tickets
    WHERE station_id = $1
      AND fault_detected_at BETWEEN $2 AND $3
      AND status = 'COMPLETED'
),
output AS (
    SELECT COUNT(*) FILTER (WHERE event_type = 'unit_produced') AS actual_output
    FROM events
    WHERE station_id = $1 AND controller_ts BETWEEN $2 AND $3
)
SELECT
    o.actual_output,
    (ideal_cycle_time_sec * o.actual_output) /
        NULLIF(
            EXTRACT(EPOCH FROM ($3::timestamptz - $2::timestamptz)) - d.dt_sec,
            0
        )                                        AS performance_ratio
    -- Denominator = Actual Production Time (PPT minus downtime)
FROM output o, downtime_sec d;
```

### OEE — Quality

```sql
SELECT
    COUNT(*) FILTER (WHERE payload->>'result' = 'pass') * 1.0 /
    NULLIF(COUNT(*), 0)                          AS quality_ratio
FROM events
WHERE event_type = 'quality_check'
  AND station_id = $1
  AND controller_ts BETWEEN $2 AND $3;
```

### OEE — Full Combined

```sql
WITH avail AS (-- availability query above --),
     perf  AS (-- corrected performance query above --),
     qual  AS (-- quality query above --)
SELECT
    avail.availability * perf.performance_ratio * qual.quality_ratio AS oee
FROM avail, perf, qual;
```

---

### MTBF (ISO 14224) — trailing interval fix

> ⚠️ **The bug:** LAG-only query drops the interval from last repair to NOW. Understates MTBF for machines that haven't failed recently.
> ✅ **The fix:** add the trailing interval before dividing.

```sql
SELECT
    machine_id,
    COUNT(*)                                                AS failure_count,
    (
        SUM(uptime_min) +
        -- Trailing interval: last repair → NOW (current operating period)
        EXTRACT(EPOCH FROM (NOW() - MAX(repair_completed_at))) / 60
    ) / NULLIF(COUNT(*), 0)                                AS mtbf_minutes
FROM (
    SELECT
        machine_id,
        repair_completed_at,
        EXTRACT(EPOCH FROM (
            fault_detected_at -
            LAG(repair_completed_at) OVER (
                PARTITION BY machine_id ORDER BY fault_detected_at
            )
        )) / 60                                            AS uptime_min
    FROM maintenance_tickets
    WHERE status = 'COMPLETED'
) sub
WHERE uptime_min IS NOT NULL
GROUP BY machine_id;

-- MTBA: same query, remove WHERE status = 'COMPLETED'
```

---

### MTTF — Component Level

```sql
SELECT
    component_type,
    COUNT(*)                                                AS replacements,
    AVG(
        EXTRACT(EPOCH FROM (fault_detected_at - component_installed_at)) / 3600
    )                                                       AS mttf_hours
FROM maintenance_tickets
JOIN component_history USING (machine_id, component_type)
WHERE status = 'COMPLETED'
GROUP BY component_type
ORDER BY mttf_hours ASC;   -- shortest MTTF = highest PM priority
```

---

## 13. Telegram & AI Layer

### Claude AI tool interface

Register these tools on the Claude AI engine:

| Tool | Arguments | Returns |
|------|-----------|---------|
| `get_oee` | `station_id, start, end` | `{availability, performance, quality, oee}` |
| `get_mtbf` | `machine_id, days` | `{mtbf_minutes, failure_count}` |
| `get_open_tickets` | `plant_id?` | List of RAISED + ACK tickets |
| `get_mttf` | `component_type?` | Ranked component MTTF list |
| `get_reboot_events` | `station_id, days` | List of boot_id transitions |

### Example NL queries the bot handles

- "What is the OEE on Line 2 for today's morning shift?"
- "Which machine has broken down most this month?"
- "What is the average engineer response time this week?"
- "Show me the MTBF trend for Machine A03 over the last 6 months."
- "Which component type has the shortest MTTF?"
- "Are there any unacknowledged maintenance tickets right now?"
- "Has Station ST07 rebooted unexpectedly this week?"

### Grafana dashboard panels

| Panel | Metric | Query source |
|-------|--------|-------------|
| OEE Gauge | Live OEE % per line | Materialized view: `oee_by_line` |
| MTBF Trend | MTBF hours, rolling 90 days | `maintenance_tickets` LAG query |
| Open Tickets | Count RAISED + ACK | `maintenance_tickets WHERE status IN (...)` |
| Downtime Heatmap | Downtime min by machine/day | `GROUP BY machine_id, date_trunc('day', ...)` |
| Response Time | Avg engineer response/week | `AVG(response_minutes) GROUP BY week` |
| Component MTTF | Lifetime ranking | `component_history JOIN tickets` |
| Reboot Events | boot_id transitions per station | `events WHERE event_type='reboot'` |

---

## 14. Implementation Checklist

### Phase 1 — Controller Foundation

- [ ] Install RS485 HAT; verify Modbus RTU comms to every slave device
- [ ] Map all slave addresses (PLC fault registers, sensor input registers, I/O coil addresses)
- [ ] Create `/etc/ctrl_id.json` with all five fields (plant, line, zone, station, boot_id) at provisioning time
- [ ] Deploy SQLite `event_log` schema v2 (all 6 identity columns)
- [ ] Implement `identity.py` — reads `/etc/ctrl_id.json`, generates UUID only if absent
- [ ] Implement `log_event()` stamping all 6 fields on every row
- [ ] Implement `modbus_poll.py` — 100 ms cyclic poll, 300 ms SW debounce for button coil
- [ ] Implement sync agent — full compound identity in MQTT payload, QoS 1, ACK-only synced flag
- [ ] Test offline: disconnect RJ45, verify polling continues and button press logs to SQLite
- [ ] Test reconnect: verify events flush in seq order, server confirms receipt, gaps backfilled
- [ ] **Test reflash scenario**: wipe SD card, re-provision, verify new `boot_id`, old data on server untouched, no silent discards
- [ ] Verify LED coil lights on button press (RS485 coil write)

### Phase 2 — Server and Database

- [ ] Deploy TimescaleDB with `events` table v2: `UNIQUE(plant_id, line_id, zone_id, station_id, boot_id, controller_seq)`
- [ ] Create index on `(station_id, boot_id, controller_seq)`
- [ ] Deploy `station_seq_state` table for gap detection
- [ ] Deploy `maintenance_tickets` with `fault_boot_id`, `request_boot_id` columns
- [ ] Implement gap detection scoped per `(station_id, boot_id)` — new boot_id = reboot, not gap
- [ ] Verify `ON CONFLICT DO NOTHING` handles replayed events across boot cycles
- [ ] Deploy EMQX with TLS + per-controller certificates
- [ ] Create OEE materialized view using corrected Performance denominator (APT, not elapsed)
- [ ] Create MTBF materialized view including trailing interval
- [ ] Create reboot_events view

### Phase 3 — Telegram and AI

- [ ] Deploy Telegram bot — alert includes plant/line/zone/station context
- [ ] Implement engineer ACK inline keyboard; record `engineer_ack_at` on tap
- [ ] Implement escalation checker (APScheduler 60 s, SLA 15 min)
- [ ] Add Claude AI tools: `get_open_tickets`, `get_mtbf`, `get_oee`, `get_reboot_events`
- [ ] Test full flow: RS485 poll detects fault → SQLite → MQTT → Telegram alert → ACK → completion → metrics
- [ ] Configure Grafana panels (OEE, MTBF, open tickets, downtime heatmap, reboot events)

### Phase 4 — Validation

- [ ] Simulate 2-hour RJ45 outage — verify `controller_ts` preserved, metrics unaffected
- [ ] Simulate SD card wipe + reflash — verify new `boot_id`, old data safe, no silent discards
- [ ] Verify `controller_ts` used (not `server_ts`) across all metric queries
- [ ] Verify OEE Performance denominator is APT — check result against manual calculation
- [ ] Verify MTBF grows over time for machine with no recent failures (trailing interval working)
- [ ] Confirm MTTR = `repair_completed_at − fault_detected_at` (not `request_raised_at`)
- [ ] Load test: 20 controllers × 10 Hz Modbus + 1 Hz event rate — no queue buildup
- [ ] Verify reboot alert fires when a new `boot_id` is seen for a known station
- [ ] Verify RS485 bus termination; all slaves respond within 100 ms poll cycle

---

## 15. Critical Rules — Do Not Break

| # | Rule | Why |
|---|------|-----|
| 1 | **Never use last-write-wins for work order state** | Stale state from offline controller will overwrite legitimate server updates on reconnect |
| 2 | **Never store `boot_id` in `event_log`** | Table recreation resets AUTOINCREMENT — `boot_id` must survive on a separate file |
| 3 | **Never mark `synced=1` before broker ACK** | Speculative marking causes permanent data loss on publish failure |
| 4 | **Always use `controller_ts`, never `server_ts`** | `server_ts` includes network latency and outage gaps; metrics become meaningless |
| 5 | **Performance denominator = APT, not elapsed clock** | Elapsed clock double-counts downtime in both Availability and Performance |
| 6 | **MTTR starts at `fault_detected_at`, not `request_raised_at`** | These can differ by minutes to hours; wrong start time = wrong reliability data |
| 7 | **MTBF must include the trailing interval** | Dropping last repair → NOW understates MTBF for healthy machines |
| 8 | **Gap detection scoped per `(station_id, boot_id)`** | Cross-boot seq comparison is meaningless; new boot_id is a reboot, not a gap |
| 9 | **`planned_minutes` from `production_plan` table, not hardcoded** | Shifts, holidays, and planned downtime change; hardcoded values corrupt OEE trend data |
| 10 | **`ON CONFLICT DO NOTHING` only safe with 6-field unique key** | 2-field key silently discards all post-reflash events from a rebooted controller |

---

*End of reference. For the full handbook with architecture diagrams and Grafana setup, see `Factory_IoT_System_Handbook_v2.pdf`.*
