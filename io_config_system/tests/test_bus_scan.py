import socket
import threading

import pytest
from fake_modbus_client import FakeModbusClient

from engine.bus_scan import parse_ip_range, scan_rtu, scan_tcp


def test_parse_ip_range_dash():
    assert parse_ip_range("192.168.10.10-192.168.10.13") == [
        "192.168.10.10", "192.168.10.11", "192.168.10.12", "192.168.10.13",
    ]


def test_parse_ip_range_comma_list():
    assert parse_ip_range("192.168.10.10, 192.168.10.20") == ["192.168.10.10", "192.168.10.20"]


def test_parse_ip_range_single():
    assert parse_ip_range("192.168.10.10") == ["192.168.10.10"]


def test_parse_ip_range_rejects_backwards_range():
    with pytest.raises(ValueError):
        parse_ip_range("192.168.10.20-192.168.10.10")


def test_scan_rtu_finds_only_responding_unit_ids():
    fake = FakeModbusClient()
    responding = {1, 5}
    for unit_id in range(1, 33):
        if unit_id not in responding:
            fake.fail_addresses.add((unit_id, 0))

    hits = scan_rtu(fake, unit_id_range=range(1, 33))

    assert {h.unit_id for h in hits} == responding
    assert all(h.host is None for h in hits)
    assert all(h.responded_ms >= 0 for h in hits)


def test_scan_tcp_finds_only_listening_hosts():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    _, port = server.getsockname()
    stop = threading.Event()

    def accept_loop():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                conn.close()
            except (socket.timeout, OSError):
                continue

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        # 127.0.0.2 is not listening; only 127.0.0.1 on our bound port is.
        hits = scan_tcp("127.0.0.1", port=port, timeout_s=0.3)
        assert [h.host for h in hits] == ["127.0.0.1"]

        hits_none = scan_tcp("127.0.0.1", port=1, timeout_s=0.2)  # closed port
        assert hits_none == []
    finally:
        stop.set()
        server.close()
        t.join(timeout=1)
