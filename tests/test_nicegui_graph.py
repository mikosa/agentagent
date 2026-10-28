from app.ui.graph import graph_options


def test_echart_graph_uses_controller_sessions_and_typed_edges():
    options = graph_options(
        [{"id": "a", "title": "Alpha", "status": "ready"}, {"id": "b", "title": "Beta", "status": "running"}],
        [{"source_id": "a", "target_id": "b", "type": "PARENT_OF"}],
    )
    series = options["series"][0]
    assert series["type"] == "graph" and series["layout"] == "force" and series["roam"]
    assert [node["id"] for node in series["data"]] == ["a", "b"]
    assert series["links"][0]["source"] == "a"
    assert series["links"][0]["target"] == "b"
