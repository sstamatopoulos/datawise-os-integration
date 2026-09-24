"""scripts/verify_platform.py: the parts that decide which entity belongs to which gate."""
import importlib.util
import sys
from pathlib import Path

from datagates.core.measurement_summary import summary_measurement_urn

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify_platform.py"
_spec = importlib.util.spec_from_file_location("verify_platform", _PATH)
verify_platform = importlib.util.module_from_spec(_spec)
sys.modules["verify_platform"] = verify_platform   # @dataclass looks its module up there
_spec.loader.exec_module(verify_platform)


def _device(urn, *properties):
    return {"id": urn, "controlledProperty": {"type": "Property", "value": list(properties)}}


def _summary(device, prop):
    urn = summary_measurement_urn(device["id"], prop)
    # What a second gate on the same upstream also writes: the same provider
    # and source. Only the id tells the two apart.
    return {"id": urn, "dataProvider": {"type": "Property", "value": "Open-Meteo"},
            "source": {"type": "Property", "value": "https://open-meteo.com"}}


def test_summaries_of_keeps_only_this_gates_devices(monkeypatch):
    # Recorded on the stack: weather_forecast and weather_observed both read
    # Open-Meteo, and with the second paused `--gate weather_forecast` failed
    # on the second's idle summaries.
    forecast = _device("urn:ngsi-ld:WeatherForecastLocation:weather_forecast-riga", "temperature", "visibility")
    observed = _device("urn:ngsi-ld:WeatherObserved:weather_observed-riga", "temperature")
    broker = [_summary(forecast, "temperature"), _summary(forecast, "visibility"), _summary(observed, "temperature")]
    monkeypatch.setattr(verify_platform, "orion", lambda path, **params: broker)

    found, missing = verify_platform.summaries_of([forecast])

    assert {e["id"] for e in found} == {summary_measurement_urn(forecast["id"], p) for p in ("temperature", "visibility")}
    assert missing == 0


def test_summaries_of_counts_a_summary_never_written(monkeypatch):
    device = _device("urn:ngsi-ld:Device:meter-1", "energy", "power")
    monkeypatch.setattr(verify_platform, "orion", lambda path, **params: [_summary(device, "energy")])

    found, missing = verify_platform.summaries_of([device])

    assert len(found) == 1
    assert missing == 1
