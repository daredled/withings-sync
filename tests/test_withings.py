import pytest

from withings_sync.cli import merge_bp_csv
from withings_sync.client import BP_COLUMNS, WithingsClient, WithingsError, _call, blood_pressure_rows


def group(grpid, date, sys=125, dia=82, pulse=58, tz="America/Santiago"):
    measures = [{"type": 10, "value": sys, "unit": 0}, {"type": 9, "value": dia, "unit": 0}]
    if pulse is not None:
        measures.append({"type": 11, "value": pulse, "unit": 0})
    return {"grpid": grpid, "date": date, "category": 1, "timezone": tz, "measures": measures}


def test_rows_use_group_timezone_and_units():
    # 2026-09-30 12:15 UTC = 09:15 in Santiago (UTC-3)
    g = group(1, 1790770500, sys=1255, dia=820)
    g["measures"][0]["unit"] = g["measures"][1]["unit"] = -1
    row = blood_pressure_rows([g])["1"]
    assert (row["date"], row["time"]) == ("2026-09-30", "09:15")
    assert (row["systolic"], row["diastolic"], row["pulse"]) == (126, 82, 58)


def test_groups_without_both_pressures_are_skipped():
    g = group(2, 1790770500)
    g["measures"] = [m for m in g["measures"] if m["type"] != 9]
    assert blood_pressure_rows([g]) == {}
    assert blood_pressure_rows([group(3, 1790770500, pulse=None)])["3"]["pulse"] == ""


def test_merge_bp_csv_upserts_by_grpid_and_sorts(tmp_path):
    path = tmp_path / "bp.csv"
    merge_bp_csv(path, blood_pressure_rows([group(5, 1790770500), group(4, 1790684100)]))
    merge_bp_csv(path, blood_pressure_rows([group(5, 1790770500, sys=130)]))
    lines = path.read_text().splitlines()
    assert lines[0] == ",".join(BP_COLUMNS)
    assert len(lines) == 3
    assert lines[1].startswith("2026-09-29") and lines[2].startswith("2026-09-30,09:15,130,")


def test_nonzero_status_raises(monkeypatch):
    monkeypatch.setattr("withings_sync.client._request_json", lambda *a, **k: {"status": 401, "error": "invalid token"})
    with pytest.raises(WithingsError, match="status 401"):
        _call("https://example.invalid", {"action": "getmeas"})


def test_code_from_redirect_url_checks_state():
    url = "http://127.0.0.1:3001/callback?code=abc&state=s1"
    assert WithingsClient.code_from_redirect_url(url, "s1") == "abc"
    with pytest.raises(WithingsError, match="state mismatch"):
        WithingsClient.code_from_redirect_url(url, "other")
