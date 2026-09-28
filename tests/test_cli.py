from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from hotel_etl.cli import main
from hotel_etl.config import load_hotels
from hotel_etl.demo import _write_ndjson, run_demo
from hotel_etl.errors import PipelineError, ValidationError
from hotel_etl.fixtures import DEMO_HOTELS, DEMO_TOKEN, fixture_api


def config(path: Path, value: object = None) -> Path:
    payload = (
        value
        if value is not None
        else {
            "hotels": [{"hotel_id": "DEMO_NORTH", "room_type_ids": ["single", "double", "suite"]}]
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_valid_configuration_and_duplicate_hotel_rejection(tmp_path: Path) -> None:
    path = config(tmp_path / "hotels.json")
    assert load_hotels(path) == (DEMO_HOTELS[0],)
    payload = {"hotels": [{"hotel_id": "one", "room_type_ids": ["a"]}] * 2}
    config(path, payload)
    with pytest.raises(ValidationError, match="unique"):
        load_hotels(path)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"hotels": []},
        {"hotels": {}},
        {"hotels": [], "typo": 1},
        {"hotels": [None]},
        {"hotels": [{"hotel_id": "one"}]},
        {"hotels": [{"hotel_id": 123, "room_type_ids": ["a"]}]},
        {"hotels": [{"hotel_id": "one", "room_type_ids": "a"}]},
        {"hotels": [{"hotel_id": "one", "room_type_ids": [False]}]},
    ],
)
def test_invalid_configuration(tmp_path: Path, payload: object) -> None:
    with pytest.raises(ValidationError):
        load_hotels(config(tmp_path / "hotels.json", payload))


def test_oversize_and_ambiguous_configuration(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_bytes(b" " * 65_537)
    with pytest.raises(ValidationError, match="64 KiB"):
        load_hotels(path)
    path.write_bytes(b'{"hotels": [], "hotels": []}')
    with pytest.raises(ValidationError, match="unambiguous"):
        load_hotels(path)


def test_demo_checks_history_and_replay_and_refuses_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "demo"
    summary = run_demo(output, days=3, first_date=date(2026, 9, 27))
    assert summary["room_types_per_hotel"] == 3
    assert summary["first_snapshot_rows"] == 18
    assert summary["rows_after_identical_replay"] == 18
    assert summary["rows_after_second_day"] == 36
    assert summary["live_cloud_verified"] is False
    assert summary["synthetic_data_only"] is True
    exported = (output / "snapshot.ndjson").read_text(encoding="utf-8")
    assert len([json.loads(line) for line in exported.splitlines()]) == 36
    assert DEMO_TOKEN not in exported
    assert json.loads((output / "summary.json").read_text(encoding="utf-8")) == summary
    with pytest.raises(ValidationError, match="Existing files kept"):
        run_demo(output, days=3, first_date=date(2026, 9, 27))
    assert (output / "snapshot.ndjson").read_text(encoding="utf-8") == exported


@pytest.mark.parametrize(
    "days,first", [(0, date(2026, 9, 27)), (367, date(2026, 9, 27)), (1, date.max)]
)
def test_demo_invalid_range_does_not_create_database(
    tmp_path: Path, days: int, first: date
) -> None:
    with pytest.raises(ValidationError):
        run_demo(tmp_path / "bad", days=days, first_date=first)
    assert not (tmp_path / "bad" / "availability.sqlite").exists()


def test_demo_acceptance_failure_is_a_pipeline_error_and_skips_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hotel_etl.demo._database_count", lambda _path: 0)
    output = tmp_path / "failed-demo"
    with pytest.raises(PipelineError, match="acceptance checks") as error:
        run_demo(output, days=1, first_date=date(2026, 9, 27))
    assert type(error.value) is PipelineError
    assert not (output / "snapshot.ndjson").exists()
    assert not (output / "summary.json").exists()


def test_demo_ignores_proxy_settings_for_its_loopback_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proxy_recorder: tuple[str, list[tuple[str, str, str]]],
) -> None:
    proxy_url, proxied = proxy_recorder
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    summary = run_demo(tmp_path / "proxied-demo", days=1, first_date=date(2026, 9, 27))
    assert summary["rows_after_second_day"] == 12
    assert proxied == []


def test_standard_library_only_demo_in_clean_subprocess(tmp_path: Path) -> None:
    # -S disables site-packages, proving the local example needs no Google SDK or pytest.
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "hotel_etl",
            "demo",
            "--days",
            "1",
            "--output-dir",
            str(tmp_path / "stdlib"),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["rows_after_second_day"] == 12


def test_sync_cli_with_actual_local_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = config(tmp_path / "hotels.json")
    with fixture_api(DEMO_HOTELS, date(2026, 9, 27)) as url:
        monkeypatch.setenv("HOTEL_API_BASE_URL", url)
        monkeypatch.setenv("HOTEL_API_TOKEN", DEMO_TOKEN)
        status = main(
            [
                "sync",
                "--config",
                str(path),
                "--days",
                "2",
                "--snapshot-at",
                "2026-09-27T06:00:00Z",
                "--sqlite",
                str(tmp_path / "snapshot.sqlite"),
            ]
        )
    captured = capsys.readouterr()
    assert status == 0
    assert json.loads(captured.out)["rows_processed"] == 6
    assert not captured.err
    assert DEMO_TOKEN not in captured.out


def test_sync_source_failure_is_redacted_and_does_not_create_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = config(tmp_path / "hotels.json")
    with fixture_api(DEMO_HOTELS, date(2026, 9, 27)) as url:
        monkeypatch.setenv("HOTEL_API_BASE_URL", url)
        monkeypatch.setenv("HOTEL_API_TOKEN", "private-wrong-token")
        status = main(
            [
                "sync",
                "--config",
                str(path),
                "--days",
                "1",
                "--sqlite",
                str(tmp_path / "missing.sqlite"),
            ]
        )
    captured = capsys.readouterr()
    assert status == 1
    assert "401" in captured.err
    assert "private-wrong-token" not in captured.err + captured.out
    assert not (tmp_path / "missing.sqlite").exists()


def test_missing_credentials_and_file_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HOTEL_API_TOKEN", raising=False)
    assert main(["sync", "--config", "missing.json", "--sqlite", str(tmp_path / "x.sqlite")]) == 1
    assert "HOTEL_API_TOKEN" in capsys.readouterr().err
    monkeypatch.setenv("HOTEL_API_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("HOTEL_API_TOKEN", "private-token")
    assert (
        main(
            [
                "sync",
                "--config",
                str(tmp_path / "absent.json"),
                "--sqlite",
                str(tmp_path / "x.sqlite"),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error"] == "Local file operation failed."


@pytest.mark.parametrize("stamp", ["not-a-date", "2026-09-27T06:00:00"])
def test_cli_rejects_invalid_or_naive_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stamp: str
) -> None:
    monkeypatch.setenv("HOTEL_API_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("HOTEL_API_TOKEN", "private-token")
    assert (
        main(
            [
                "sync",
                "--config",
                "not-needed.json",
                "--snapshot-at",
                stamp,
                "--sqlite",
                str(tmp_path / "x.sqlite"),
            ]
        )
        == 1
    )
    assert "private-token" not in capsys.readouterr().err


def test_cli_demo_outputs_machine_readable_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["demo", "--days", "1", "--output-dir", str(tmp_path / "cli-demo")]) == 0
    assert json.loads(capsys.readouterr().out)["rows_after_second_day"] == 12


def test_failed_export_keeps_prior_file_and_cleans_its_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "snapshot.ndjson"
    path.write_text("keep", encoding="utf-8")

    def fail(*args: object) -> None:
        raise OSError("simulated full disk")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        _write_ndjson(path, [])
    assert path.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.iterdir()) == [path]


def test_bigquery_cli_wiring_uses_validated_rows_without_real_cloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import hotel_etl.storage.bigquery as cloud

    calls: list[object] = []

    class FakeCloudSink:
        def __init__(self, table_id: str, *, location: str) -> None:
            calls.append((table_id, location))

        def write(self, rows: object) -> None:
            calls.append(rows)

    monkeypatch.setattr(cloud, "BigQuerySink", FakeCloudSink)
    path = config(tmp_path / "hotels.json")
    with fixture_api(DEMO_HOTELS, date(2026, 9, 27)) as url:
        monkeypatch.setenv("HOTEL_API_BASE_URL", url)
        monkeypatch.setenv("HOTEL_API_TOKEN", DEMO_TOKEN)
        status = main(
            [
                "sync",
                "--config",
                str(path),
                "--days",
                "1",
                "--snapshot-at",
                "2026-09-27T06:00:00Z",
                "--start-date",
                "2026-09-27",
                "--bigquery",
                "demo-project.hotel_demo.availability_snapshot",
                "--location",
                "EU",
            ]
        )
    assert status == 0
    assert calls[0] == ("demo-project.hotel_demo.availability_snapshot", "EU")
    assert len(calls[1]) == 3
    assert json.loads(capsys.readouterr().out)["sink"] == "bigquery"


def test_module_entry_point_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import runpy

    monkeypatch.setattr(sys, "argv", ["hotel-etl", "--help"])
    with pytest.raises(SystemExit) as exit_status:
        runpy.run_module("hotel_etl", run_name="__main__")
    assert exit_status.value.code == 0
    assert "Import validated daily hotel availability snapshots." in capsys.readouterr().out


@pytest.mark.parametrize(
    "data",
    [b'{"hotels": ["secret-canary",', b"\xff", b'{"hotels": NaN}', b'{"hotels": Infinity}'],
    ids=["malformed", "invalid-utf8", "nan", "infinity"],
)
def test_configuration_decoder_errors_keep_the_existing_sanitized_message(
    tmp_path: Path, data: bytes
) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(data)
    with pytest.raises(ValidationError) as error:
        load_hotels(path)
    assert str(error.value) == "Hotel configuration is not valid, unambiguous JSON."
    assert error.value.__suppress_context__ is True


def test_configuration_accepts_exactly_64_kib(tmp_path: Path) -> None:
    path = config(tmp_path / "hotels.json")
    data = path.read_bytes()
    path.write_bytes(data + b" " * (65_536 - len(data)))
    assert load_hotels(path) == (DEMO_HOTELS[0],)


def test_cli_dispatches_demo_options_without_owning_demo_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hotel_etl import demo

    output = tmp_path / "delegated-demo"
    calls: list[tuple[Path, int, date]] = []
    summary: dict[str, object] = {"status": "ok", "synthetic_data_only": True}

    def run(output: Path, *, days: int, first_date: date) -> dict[str, object]:
        calls.append((output, days, first_date))
        return summary

    monkeypatch.setattr(demo, "run_demo", run)
    assert (
        main(
            [
                "demo",
                "--output-dir",
                str(output),
                "--days",
                "2",
                "--snapshot-date",
                "2026-02-03",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert calls == [(output, 2, date(2026, 2, 3))]
    assert json.loads(captured.out) == summary
    assert captured.err == ""
    assert not output.exists()


def test_keyboard_interrupt_has_a_clear_error_and_exit_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("hotel_etl.demo.run_demo", interrupt)
    assert main(["demo"]) == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "error",
        "error": "Interrupted. Check the destination before retrying.",
    }
    assert "Traceback" not in captured.err


def test_sync_help_explains_dates_and_destination(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_status:
        main(["sync", "--help"])
    assert exit_status.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "First stay date" in help_text
    assert "1 to 366 days" in help_text
    assert "parent directory must exist" in help_text
