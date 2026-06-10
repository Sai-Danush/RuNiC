import math

import pytest

from runic.pace import (
    build_pace_model,
    minetti_cost_factor,
    parse_distance,
    parse_duration,
    parse_pb,
    riegel_predict,
)


def test_parse_duration_variants():
    assert parse_duration("22:30") == 1350
    assert parse_duration("1:00:00") == 3600
    assert parse_duration("90") == 90


def test_parse_distance_variants():
    assert parse_distance("5k") == 5000
    assert parse_distance("10km") == 10000
    assert parse_distance("half") == pytest.approx(21097.5)
    assert parse_distance("3000") == 3000


def test_parse_pb():
    assert parse_pb("5k=22:30") == (5000, 1350)


def test_riegel_predicts_slower_for_longer():
    # 5k in 22:30 -> 10k should be a bit more than double (fatigue).
    t10k = riegel_predict(5000, 1350, 10000)
    assert t10k > 2700  # > exactly double
    assert t10k == pytest.approx(1350 * 2 ** 1.06, rel=1e-6)


def test_minetti_flat_is_unity_and_uphill_costlier():
    assert minetti_cost_factor(0.0) == pytest.approx(1.0, abs=1e-9)
    assert minetti_cost_factor(8.0) > 1.3
    assert minetti_cost_factor(-5.0) < 1.0


def test_build_pace_model_from_pb():
    model = build_pace_model(pbs=["5k=22:30"], target_distance_m=5000)
    expected_speed = 5000 / 1350
    assert model.base_speed_mps == pytest.approx(expected_speed, rel=1e-6)
    assert model.cadence_spm == 170.0  # default


def test_build_pace_model_requires_a_source():
    with pytest.raises(ValueError):
        build_pace_model()


def test_cadence_override_wins():
    model = build_pace_model(pbs=["5k=22:30"], target_distance_m=5000,
                             cadence_override=182)
    assert model.cadence_spm == 182
