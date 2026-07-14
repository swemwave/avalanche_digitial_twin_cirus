from __future__ import annotations

from app.services.terrain import categorize_osm_feature


def test_osm_infrastructure_categorization() -> None:
    assert categorize_osm_feature({"highway": "service"}, "LineString") == "roads"
    assert categorize_osm_feature({"highway": "path"}, "LineString") == "trails"
    assert categorize_osm_feature({"building": "yes"}, "Polygon") == "buildings"
    assert categorize_osm_feature({"railway": "rail"}, "LineString") == "railways"
    assert categorize_osm_feature({"power": "line"}, "LineString") == "power"
    assert categorize_osm_feature({"waterway": "stream"}, "LineString") == "waterways"
