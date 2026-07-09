import pytest
from conftest import load_seed

from engine import config_store
from engine.event_store import fetch_events, init_db
from engine.identity_store import IdentityUpdateError, update_identity


def _setup(tmp_path):
    identity_path = tmp_path / "ctrl_id.json"
    config_store.atomic_write_json(identity_path, load_seed("ctrl_id.seed.json"))
    db_path = tmp_path / "event_log.db"
    init_db(db_path)
    return identity_path, db_path


def test_update_editable_fields_succeeds(tmp_path):
    identity_path, db_path = _setup(tmp_path)
    new_ident = update_identity(
        identity_path, db_path, {"line_id": "L09"}, updated_by="admin1", confirm=True,
    )
    assert new_ident["line_id"] == "L09"
    assert new_ident["boot_id"] == "f47ac10b-58cc-4372-a567-0e02b2c3d479"  # untouched
    on_disk = config_store.read_json(identity_path)
    assert on_disk == new_ident


def test_boot_id_in_request_is_rejected_not_ignored(tmp_path):
    identity_path, db_path = _setup(tmp_path)
    original = config_store.read_json(identity_path)

    with pytest.raises(IdentityUpdateError) as exc:
        update_identity(
            identity_path, db_path,
            {"line_id": "L09", "boot_id": "11111111-1111-1111-1111-111111111111"},
            updated_by="admin1", confirm=True,
        )
    assert any("boot_id" in p for p in exc.value.problems)
    assert config_store.read_json(identity_path) == original  # nothing written


def test_missing_confirm_is_rejected(tmp_path):
    identity_path, db_path = _setup(tmp_path)
    original = config_store.read_json(identity_path)

    with pytest.raises(IdentityUpdateError) as exc:
        update_identity(identity_path, db_path, {"line_id": "L09"}, updated_by="admin1", confirm=False)
    assert any("confirm" in p for p in exc.value.problems)
    assert config_store.read_json(identity_path) == original


def test_unknown_field_is_rejected(tmp_path):
    identity_path, db_path = _setup(tmp_path)
    with pytest.raises(IdentityUpdateError) as exc:
        update_identity(identity_path, db_path, {"favorite_color": "blue"}, updated_by="admin1", confirm=True)
    assert any("unknown identity field" in p for p in exc.value.problems)


def test_identity_change_event_logged_under_old_identity(tmp_path):
    identity_path, db_path = _setup(tmp_path)
    old = config_store.read_json(identity_path)

    update_identity(identity_path, db_path, {"line_id": "L09"}, updated_by="admin1", confirm=True)

    events = fetch_events(db_path)
    change_events = [e for e in events if e["event_type"] == "identity_change"]
    assert len(change_events) == 1
    event = change_events[0]
    # Stamped with the OLD identity (see identity_store.py comment) --
    # the event is the last thing filed under the identity being replaced.
    assert event["line_id"] == old["line_id"]
    assert event["boot_id"] == old["boot_id"]

    import json
    payload = json.loads(event["payload"])
    assert payload["updated_by"] == "admin1"
    assert payload["changed"]["line_id"] == {"old": old["line_id"], "new": "L09"}


def test_no_op_update_with_confirm_logs_no_change(tmp_path):
    identity_path, db_path = _setup(tmp_path)
    original = config_store.read_json(identity_path)

    update_identity(identity_path, db_path, {"line_id": original["line_id"]}, updated_by="admin1", confirm=True)

    import json
    events = fetch_events(db_path)
    change_events = [e for e in events if e["event_type"] == "identity_change"]
    assert len(change_events) == 1
    assert json.loads(change_events[0]["payload"])["changed"] == {}
