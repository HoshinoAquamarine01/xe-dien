import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import app as routing


def test_build_candidate_indices_includes_backward_nodes_and_prunes():
    # Create simple nodes with km positions
    nodes = [
        {'km_position': 0},
        {'km_position': 50},
        {'km_position': 95},
        {'km_position': 100},
        {'km_position': 105},
        {'km_position': 110},
        {'km_position': 500}
    ]
    # Use a constrained config to force pruning: 75kWh battery, 100 kWh/100km consumption -> ~82.5km range
    config = {'battery_capacity': 75, 'consumption': 100, 'backtrack_window_km': 10.0}

    # Current index 3 (km 100)
    candidates = routing.build_candidate_indices(3, nodes, config)

    # Should include backward node at km 95 (index 2)
    assert 2 in candidates
    # Must include forward nodes that are within range (105,110)
    assert 4 in candidates and 5 in candidates

    # Node at km 500 is out of theoretical range and should NOT be included
    assert 6 not in candidates
