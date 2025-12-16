import os
import sys
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as routing


class DummyResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {"status": "OK", "routes": []}

    def json(self):
        return self._data


def test_goong_gets_avoid_param(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured['url'] = url
        captured['params'] = params
        return DummyResponse()

    monkeypatch.setattr(routing.requests, 'get', fake_get)

    user_filters = {'avoid_tolls': True, 'avoid_highways': True, 'avoid_ferries': False}
    routing.get_route_from_goong((10.0,106.0), (10.5,106.5), vehicle='car', user_filters=user_filters)

    assert 'avoid' in captured['params']
    assert 'toll' in captured['params']['avoid']
    assert 'motorway' in captured['params']['avoid']


def test_mapbox_gets_exclude_and_alternatives(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured['url'] = url
        captured['params'] = params
        return DummyResponse()

    monkeypatch.setattr(routing.requests, 'get', fake_get)

    user_filters = {'avoid_tolls': True, 'avoid_highways': False, 'avoid_ferries': True}
    routing.get_route_from_mapbox((10.0,106.0), (10.5,106.5), user_filters=user_filters)

    assert 'exclude' in captured['params']
    # exclude should contain toll and ferry
    assert 'toll' in captured['params']['exclude']
    assert 'ferry' in captured['params']['exclude']
    assert captured['params'].get('alternatives') == 'true'


def test_get_route_passes_options_to_engines(monkeypatch):
    captured = {}

    def fake_goong(start, end, vehicle='car', user_filters=None):
        captured['goong'] = user_filters
        return []

    def fake_mapbox(start, end, user_filters=None):
        captured['mapbox'] = user_filters
        return []

    monkeypatch.setattr(routing, 'get_route_from_goong', fake_goong)
    monkeypatch.setattr(routing, 'get_route_from_mapbox', fake_mapbox)

    routes, err = routing.get_route((10.0,106.0), (10.5,106.5), options={'avoid_tolls': True, 'avoid_ferries': False})

    # The engines should have received the user_filters dict
    assert 'goong' in captured and isinstance(captured['goong'], dict)
    assert captured['goong'].get('avoid_tolls') is True
    assert 'mapbox' in captured and isinstance(captured['mapbox'], dict)


def test_goong_violation_triggers_mapbox_fallback(monkeypatch):
    # Simulate Goong returning a route but violation detection finds tolls
    def fake_goong(start, end, vehicle='car', user_filters=None):
        return [{'geometry': 'encodedpoly'}]

    def fake_mapbox(start, end, user_filters=None):
        return [{'geometry': 'alt-encoded'}]

    monkeypatch.setattr(routing, 'get_route_from_goong', fake_goong)
    monkeypatch.setattr(routing, 'get_route_from_mapbox', fake_mapbox)
    # Force violation detection to return something
    monkeypatch.setattr(routing, 'find_toll_stations_on_route', lambda coords, max_distance_km=0.1: [{'station': {}}])
    # Ensure polyline.decode returns coords so violation detection runs
    monkeypatch.setattr(routing.polyline, 'decode', lambda s: [(10.0,106.0), (10.1,106.1)])

    routes, err = routing.get_route((10.0,106.0), (10.5,106.5), options={'avoid_tolls': True})
    # Should have used mapbox fallback
    assert routes and routes[0].get('geometry') == 'alt-encoded'
