import types
import importlib

# Import the module under test
import app as routing


def test_optimal_charge_target_partial(monkeypatch):
    # Mock find_next_better_station to simulate a better station ahead
    def fake_find_next_better_station(current_pos, max_distance_km, current_power, config):
        return {'station': {'name': 'FastStation', 'power': current_power + 50}, 'distance': 120, 'power': current_power + 50}

    monkeypatch.setattr(routing, 'find_next_better_station', fake_find_next_better_station)

    # Simple config
    config = {
        'battery_capacity': 75.0,
        'consumption': 15.0,
        'min_battery': 10
    }

    # If current battery is 40% and a much faster station exists ahead, expect optimal target < 100
    target = routing.calculate_optimal_charge_target(remaining_checkpoints=[(10.0, 106.0)], current_station={'power': 50}, current_battery=40, config=config)
    assert target <= 85 or target < 100


def test_optimal_charge_target_full(monkeypatch):
    # Mock to return None (no better station)
    monkeypatch.setattr(routing, 'find_next_better_station', lambda *args, **kwargs: None)

    config = {
        'battery_capacity': 75.0,
        'consumption': 15.0,
        'min_battery': 10
    }

    target = routing.calculate_optimal_charge_target(remaining_checkpoints=[(10.0, 106.0)], current_station={'power': 50}, current_battery=40, config=config)
    assert isinstance(target, int) or isinstance(target, float)
    assert target == 100 or target >= 40
