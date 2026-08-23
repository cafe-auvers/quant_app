import threading

from sqlalchemy import create_engine, text

from src.services import coordination_change_pulse as pulse


def test_transactional_write_advances_internal_generation_without_polling():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE trade_cards (id INTEGER PRIMARY KEY)"))
    pulse.install_coordination_change_tracking(engine)

    initial = pulse.coordination_change_generation(engine)
    with engine.connect() as conn:
        conn.execute(text("SELECT * FROM trade_cards")).fetchall()
    assert pulse.coordination_change_generation(engine) == initial

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO trade_cards (id) VALUES (1)"))

    assert pulse.coordination_change_generation(engine) == initial + 1


def test_local_change_event_is_stable_until_acknowledged():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE operator_commands (id INTEGER PRIMARY KEY)"))
    pulse.install_coordination_change_tracking(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO operator_commands (id) VALUES (1)"))

    first = pulse.stage_local_change_event(engine, device_id="laptop-id")
    second = pulse.stage_local_change_event(engine, device_id="laptop-id")

    assert first
    assert second == first
    assert pulse.acknowledge_local_change_event(engine, first) is True
    assert pulse.stage_local_change_event(engine, device_id="laptop-id") == ""


def test_remote_event_advances_generation_once_without_echoing():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    initial = pulse.coordination_change_generation(engine)

    assert pulse.mark_remote_coordination_change(engine, "pc:event-1") is True
    assert pulse.mark_remote_coordination_change(engine, "pc:event-1") is False
    assert pulse.coordination_change_generation(engine) == initial + 1
    assert pulse.stage_local_change_event(engine, device_id="laptop-id") == ""


def test_runtime_timestamp_heartbeat_is_ignored_but_state_change_is_dirty():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE runtime_device_state "
                "(device_id TEXT PRIMARY KEY, state TEXT, updated_at TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO runtime_device_state (device_id, state, updated_at) "
                "VALUES ('pc', 'STARTING', 'old')"
            )
        )
    pulse.install_coordination_change_tracking(engine)
    initial = pulse.coordination_change_generation(engine)

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runtime_device_state SET updated_at = 'new' "
                "WHERE device_id = 'pc'"
            )
        )
    assert pulse.coordination_change_generation(engine) == initial

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runtime_device_state SET state = 'ACTIVE', updated_at = 'newer' "
                "WHERE device_id = 'pc'"
            )
        )
    assert pulse.coordination_change_generation(engine) == initial + 1


def test_change_pulse_files_round_trip_without_database_access(monkeypatch, tmp_path):
    outbound = tmp_path / "outbound.json"
    inbound = tmp_path / "inbound.json"
    monkeypatch.setattr(pulse, "OUTBOUND_CHANGE_PULSE_FILE", outbound)
    monkeypatch.setattr(pulse, "INBOUND_CHANGE_PULSE_FILE", inbound)

    assert pulse.publish_outbound_change_pulse("pc:event-2") is True
    assert pulse.read_outbound_change_pulse() == "pc:event-2"
    assert pulse.record_inbound_change_pulse("laptop:event-3") is True
    assert pulse.read_inbound_change_pulse() == "laptop:event-3"
    assert pulse.record_inbound_change_pulse("bad event with spaces") is False


def test_pc_listener_routes_change_tokens_without_tidb(monkeypatch, tmp_path):
    from scripts import pc_remote_control_listener as listener
    from src.services import pc_remote_control as client

    monkeypatch.setattr(
        pulse, "OUTBOUND_CHANGE_PULSE_FILE", tmp_path / "outbound.json"
    )
    monkeypatch.setattr(
        pulse, "INBOUND_CHANGE_PULSE_FILE", tmp_path / "inbound.json"
    )
    monkeypatch.setattr(listener, "TOKEN", "test-secret")
    assert pulse.publish_outbound_change_pulse("pc:event-4")

    server = listener._Server(("127.0.0.1", 0), listener._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    monkeypatch.setattr(client, "_pc_host", lambda: host)
    monkeypatch.setattr(client, "_pc_port", lambda: port)
    monkeypatch.setattr(client, "_token", lambda: "test-secret")
    try:
        status = client.check_pc_listener(timeout=1.0)
        assert status.status == client.PcStatus.ON
        assert status.coordination_change_pulse_supported is True
        assert status.coordination_change_event_id == "pc:event-4"
        assert client.notify_pc_coordination_change(
            "laptop:event-5", timeout=1.0
        )
        assert pulse.read_inbound_change_pulse() == "laptop:event-5"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)
