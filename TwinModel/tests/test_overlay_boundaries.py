"""UI boundary labels must follow reference direction, including reversed roads."""
import pytest
from tools.geo_overlay import lane_band_features


@pytest.mark.parametrize('reverse', [False, True])
def test_boundary_sides_and_inner_outer(reverse):
    coords = [(0, 0), (20, 0)]
    if reverse:
        coords.reverse()
    roads = {'features': [{'properties': {
        'id': 'r1', 'name': 'Test Street', 'osm_way_ids': [123],
        'lanes': [{'id': 1, 'type': 'driving', 'width': 3},
                  {'id': 2, 'type': 'sidewalk', 'width': 2},
                  {'id': -1, 'type': 'driving', 'width': 4}]},
        'geometry': {'type': 'LineString', 'coordinates': coords}}]}
    plain = lane_band_features(roads)
    detailed = lane_band_features(roads, include_boundaries=True)
    assert len(plain) == 3
    assert [f for f in detailed if 'boundary' not in f['properties']] == plain
    edges = [f for f in detailed if 'boundary' in f['properties']]
    assert len(edges) == 6
    for f in edges:
        p = f['properties']
        assert p['road_id'] == 'r1' and p['osm_way_ids'] == [123]
        assert p['side'] == ('left' if p['lane_id'] > 0 else 'right')
        widths = {1: (0, 3), 2: (3, 5), -1: (0, -4)}
        y = widths[p['lane_id']][p['boundary'] == 'outer'] * (-1 if reverse else 1)
        assert all(c[1] == pytest.approx(y) for c in f['geometry']['coordinates'])
