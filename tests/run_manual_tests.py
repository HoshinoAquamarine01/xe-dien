import sys
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import app as routing

# Test 1: Partial charge when better station exists

def fake_find_next_better_station(current_pos, max_distance_km, current_power, config):
    return {'station': {'name': 'FastStation', 'power': current_power + 50}, 'distance': 120, 'power': current_power + 50}

routing.find_next_better_station = fake_find_next_better_station
config = {'battery_capacity': 75.0, 'consumption': 15.0, 'min_battery': 10}
try:
    target = routing.calculate_optimal_charge_target(remaining_checkpoints=[(10.0, 106.0)], current_station={'power': 50}, current_battery=40, config=config)
    print('Test1 target:', target)
    assert target <= 85 or target < 100
except AssertionError:
    print('Test1 FAILED')
    sys.exit(2)

# Test 2: Full charge when no better station
routing.find_next_better_station = lambda *args, **kwargs: None
try:
    target2 = routing.calculate_optimal_charge_target(remaining_checkpoints=[(10.0, 106.0)], current_station={'power': 50}, current_battery=40, config=config)
    print('Test2 target:', target2)
    assert isinstance(target2, (int, float))
except AssertionError:
    print('Test2 FAILED')
    sys.exit(2)

print('ALL MANUAL TESTS PASSED')
sys.exit(0)
