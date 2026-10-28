"""ECharts force graph options from durable controller projections."""


EDGE_COLORS = {
    "PARENT_OF": "#a5e0bf", "SIBLING_OF": "#92b8d6",
    "DEPENDS_ON": "#e9c17c", "RELATED_TO": "#9db5b8",
    "FORKED_FROM": "#6e8289",
}
STATUS_COLORS = {"running": "#e9c17c", "held": "#ff927d", "failed": "#ff927d"}


def graph_options(sessions, edges):
    nodes = [{"id": s["id"], "name": s["title"], "value": s["status"],
              "symbolSize": 28 if s["status"] == "running" else 23,
              "itemStyle": {"color": STATUS_COLORS.get(s["status"], "#a5e0bf")}}
             for s in sessions if s["status"] != "archived"]
    visible = {node["id"] for node in nodes}
    links = [{"source": e["source_id"], "target": e["target_id"], "value": e["type"],
              "lineStyle": {"color": EDGE_COLORS.get(e["type"], "#9db5b8"),
                            "width": 2 if e["type"] == "PARENT_OF" else 1,
                            "type": "dashed" if e["type"] == "FORKED_FROM" else "solid"}}
             for e in edges if e["source_id"] in visible and e["target_id"] in visible]
    return {"backgroundColor": "transparent", "tooltip": {"trigger": "item"},
            "series": [{"type": "graph", "layout": "force", "roam": True,
                        "draggable": True, "data": nodes, "links": links,
                        "label": {"show": True, "position": "bottom", "color": "#f2f6f4"},
                        "edgeLabel": {"show": False},
                        "force": {"repulsion": 280, "edgeLength": 130, "gravity": 0.08}}]}
