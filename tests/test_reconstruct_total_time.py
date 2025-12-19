import types
import pytest
import app as routing


def test_reconstruct_total_time_computed_from_distance(monkeypatch):
    # Prepare a fake route: total_route_km = 100
    route_km_cumulative = [0, 100]
    route_coords = [(10.0, 106.0), (11.0, 107.0)]

    # Create a fake goal_node (single node path) with g_cost intentionally NOT representing time
    goal_node = types.SimpleNamespace()
    goal_node.parent = None
    goal_node.charged_at = None
    goal_node.charge_time = 0
    goal_node.charge_amount = 0
    goal_node.battery = 80
    goal_node.node_id = 0
    # Set g_cost to a value that is NOT time (e.g., a cost metric)
    goal_node.g_cost = 12345

    # Provide minimal nodes list and config
    nodes = []
    config = routing.EV_CONFIG.copy() if hasattr(routing, 'EV_CONFIG') else {'battery_capacity': 75, 'consumption': 20}

    # Monkeypatch external lookups used by reconstruct_astar_result to avoid side-effects
    monkeypatch.setattr(routing, 'find_toll_stations_on_route', lambda rc, max_distance_km=0.1: [])
    monkeypatch.setattr(routing, 'find_ferries_on_route', lambda rc, max_distance_km=0.2: [])
    monkeypatch.setattr(routing, 'find_highway_stations_on_route', lambda rc, max_distance_km=0.1: [])

    # route_avg_speed = 50 km/h -> driving time should be (100/50)*60 = 120 minutes
    result, err = routing.reconstruct_astar_result(goal_node, nodes, route_coords, route_km_cumulative, config, user_filters={}, route_avg_speed=50)

    assert err is None
    assert result is not None
    assert 'driving_time' in result and 'charging_time' in result and 'total_time' in result

    assert pytest.approx(result['driving_time'], rel=1e-3) == 120.0
    assert pytest.approx(result['charging_time'], rel=1e-3) == 0.0
    assert pytest.approx(result['total_time'], rel=1e-3) == 120.0
