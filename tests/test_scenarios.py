"""Every simulator scenario must end the way its `expect:` block says."""

from pathlib import Path

import pytest

from sim import runner

SCENARIOS = sorted(p.stem for p in (Path(__file__).resolve().parents[1] / "sim" / "scenarios").glob("*.yml"))


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_scenario(scenario_id):
    result = runner.run_scenario(runner.load_scenario(scenario_id))
    assert not result.failures, result.failures


def test_hold_warning_is_not_repeated_every_3_hours():
    result = runner.run_scenario(runner.load_scenario("03-anr-regression-hold"))
    warnings = [e for e in result.slack.entries if "needs a human look" in e.text]
    assert len(warnings) == 1


def test_grafana_webhook_halts_within_minutes():
    result = runner.run_scenario(runner.load_scenario("05-grafana-alert-then-resume"))
    halt = next(e for e in result.slack.entries if "HALTED" in e.text)
    assert halt.at.strftime("%a %H:%M") == "Sat 10:02"
