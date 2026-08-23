"""Live integration tests against a running QGIS + QGIS MCP plugin.

Skipped entirely when no plugin server is listening on localhost:9876.
Uses the real client implementation from src/qgis_mcp so the wire protocol
(length-prefixed framing + request-id correlation) is what gets tested.
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from qgis_mcp.qgis_socket_client import QgisMCPClient  # noqa: E402


# --- Pytest Fixtures ---

@pytest.fixture(scope="module")
def client():
    """Provides a connected client instance. Skips if server is not running."""
    c = QgisMCPClient()
    if not c.connect():
        pytest.skip("QGIS MCP Server is not running on localhost:9876. Please start QGIS and the plugin.")
    yield c
    c.disconnect()

@pytest.fixture(scope="module")
def setup_test_data(client):
    """Creates a temporary memory layer in QGIS for testing."""
    layer_name = f"test_layer_{uuid.uuid4().hex[:8]}"

    setup_code = f"""
from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY, QgsProject

layer = QgsVectorLayer("Point?crs=epsg:4326&field=id:integer&field=name:string", "{layer_name}", "memory")
if not layer.isValid():
    raise Exception("Failed to create memory layer")

pr = layer.dataProvider()
features = []
for i in range(5):
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(i, i)))
    f.setAttributes([i, f"Feature {{i}}"])
    features.append(f)
pr.addFeatures(features)

QgsProject.instance().addMapLayer(layer)
print(layer.id())
    """

    result = client.execute_code(setup_code)
    assert result.get("status") == "success", f"Failed to setup test data: {result}"

    layers_resp = client.get_layers()
    layers = layers_resp.get("result", [])
    target_layer = next((l for l in layers if l["name"] == layer_name), None)
    assert target_layer is not None, \
        f"Test layer '{layer_name}' not found. Available: {[l['name'] for l in layers]}"

    yield target_layer["id"]

    # Cleanup
    client.remove_layer(target_layer["id"])


# --- Basic connectivity ---

def test_ping(client):
    """Test basic connectivity (id echoed by the server is part of the reply)."""
    resp = client.ping()
    assert resp["status"] == "success"
    assert resp["result"] == {"pong": True}

def test_response_carries_request_id(client):
    """The server must echo the request id for reply correlation."""
    resp = client.send_command("ping")
    assert isinstance(resp.get("id"), int)

def test_get_qgis_info(client):
    """Test retrieving system info."""
    resp = client.get_qgis_info()
    assert resp["status"] == "success"
    assert "qgis_version" in resp["result"]


# --- Timeout / desync recovery (the worst-issue regression test) ---

def test_stale_reply_does_not_desync(client):
    """A call that outlives the client timeout must NOT poison later calls.

    Previously the late reply sat in the socket and was returned as the next
    call's result (render_map returning execute_code stdout). With request-id
    correlation + close-on-timeout, the next call must get its own reply.
    """
    slow = client.execute_code("import time; time.sleep(2); print('slow-marker')",
                               timeout=0.5)
    assert slow["status"] == "error"
    assert "timed out" in slow["message"].lower()

    # Give QGIS time to finish the sleep and flush the stale reply.
    time.sleep(2.5)

    resp = client.ping()
    assert resp["status"] == "success"
    assert resp["result"] == {"pong": True}, \
        f"Got a stale reply instead of the ping response: {resp}"

def test_reconnect_after_disconnect(client):
    """The client must transparently reconnect in a single call."""
    client.disconnect()
    resp = client.ping()
    assert resp["status"] == "success"
    assert resp["result"] == {"pong": True}


# --- Async job API ---

def test_submit_and_poll_job(client):
    """submit_code returns immediately; poll_job retrieves the result."""
    submitted = client.submit_code("print('job-output-marker')")
    assert submitted["status"] == "success"
    job_id = submitted["result"]["job_id"]
    assert submitted["result"]["status"] == "pending"

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        polled = client.poll_job(job_id)
        assert polled["status"] == "success"
        if polled["result"]["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert polled["result"]["status"] == "done"
    assert "job-output-marker" in polled["result"]["result"]["stdout"]

def test_poll_unknown_job(client):
    resp = client.poll_job("job_does_not_exist")
    assert resp["status"] == "error"


# --- Layer tree API ---

def test_layer_tree_roundtrip(client, setup_test_data):
    """add_group / move_node / get_layer_tree / set_node_visibility."""
    group_name = f"test_group_{uuid.uuid4().hex[:8]}"
    try:
        created = client.add_group(group_name)
        assert created["status"] == "success"
        assert created["result"]["path"] == group_name

        # Nested group
        nested = client.add_group("nested", parent_path=group_name)
        assert nested["status"] == "success"
        assert nested["result"]["path"] == f"{group_name}/nested"

        # Move the test layer into the nested group
        moved = client.move_node(layer_id=setup_test_data,
                                 target_group_path=f"{group_name}/nested")
        assert moved["status"] == "success"

        # The tree must reflect the structure
        tree = client.get_layer_tree()
        assert tree["status"] == "success"

        def find_group(nodes, name):
            for n in nodes:
                if n["type"] == "group" and n["name"] == name:
                    return n
            return None

        top = find_group(tree["result"]["tree"], group_name)
        assert top is not None
        inner = find_group(top["children"], "nested")
        assert inner is not None
        layer_ids = [c.get("layer_id") for c in inner["children"]]
        assert setup_test_data in layer_ids

        # Visibility: uncheck the top group, then enable the nested group
        # with ancestor checking - the top group must become checked again.
        off = client.set_node_visibility(False, group_path=group_name)
        assert off["status"] == "success"
        assert off["result"]["checked"] is False

        on = client.set_node_visibility(True, group_path=f"{group_name}/nested",
                                        check_ancestors=True)
        assert on["status"] == "success"
        assert on["result"]["visible"] is True

        tree = client.get_layer_tree()
        top = find_group(tree["result"]["tree"], group_name)
        assert top["checked"] is True, "Ancestor group was not re-checked"

        # Move the layer back to the root so cleanup can remove it normally
        back = client.move_node(layer_id=setup_test_data)
        assert back["status"] == "success"
    finally:
        client.execute_code(f"""
from qgis.core import QgsProject
root = QgsProject.instance().layerTreeRoot()
g = root.findGroup("{group_name}")
if g is not None:
    root.removeChildNode(g)
""")

def test_move_group_into_itself_rejected(client):
    group_name = f"test_group_{uuid.uuid4().hex[:8]}"
    try:
        client.add_group(group_name)
        resp = client.move_node(group_path=group_name,
                                target_group_path=group_name)
        assert resp["status"] == "error"
    finally:
        client.execute_code(f"""
from qgis.core import QgsProject
root = QgsProject.instance().layerTreeRoot()
g = root.findGroup("{group_name}")
if g is not None:
    root.removeChildNode(g)
""")


# --- Canvas extent ---

def test_get_extent(client):
    resp = client.get_extent()
    assert resp["status"] == "success"
    r = resp["result"]
    for key in ("xmin", "ymin", "xmax", "ymax", "crs", "scale"):
        assert key in r
    assert r["xmax"] > r["xmin"]

def test_set_extent_and_restore(client):
    original = client.get_extent()["result"]
    try:
        # refresh=False: no repaint needed for the assertion, and crash-safe
        # if the live project holds ECW/ODBC layers.
        resp = client.set_extent(0, 0, 10, 10, refresh=False)
        assert resp["status"] == "success"
        r = resp["result"]
        # Canvas adjusts to aspect ratio; the requested bbox must be contained.
        assert r["xmin"] <= 0 and r["xmax"] >= 10
        assert r["ymin"] <= 0 and r["ymax"] >= 10
    finally:
        client.set_extent(original["xmin"], original["ymin"],
                          original["xmax"], original["ymax"], refresh=False)


# --- Render timing ---

def test_render_map_reports_duration(client, setup_test_data, tmp_path):
    out = str(tmp_path / "render_test.png").replace("\\", "/")
    resp = client.send_command("render_map", {
        "path": out, "width": 200, "height": 150,
        "layer_ids": [setup_test_data],
    })
    assert resp["status"] == "success"
    r = resp["result"]
    assert r["rendered"] is True
    assert isinstance(r["render_seconds"], (int, float))
    assert r["render_seconds"] >= 0
    assert r["layer_count"] == 1
    assert os.path.exists(out)


# --- Feature retrieval (pre-existing behavior) ---

def test_get_layers_basic(client, setup_test_data):
    """Test layer listing."""
    resp = client.get_layers()
    assert resp["status"] == "success"
    ids = [l["id"] for l in resp["result"]]
    assert setup_test_data in ids

def test_feature_limit(client, setup_test_data):
    """Test that the 'limit' parameter works."""
    resp = client.send_command("get_layer_features", {
        "layer_id": setup_test_data,
        "limit": 3,
        "include_geometry": False
    })
    assert resp["status"] == "success"
    assert len(resp["result"]["features"]) == 3

def test_geometry_exclusion(client, setup_test_data):
    """Test that geometry is excluded by default."""
    resp = client.send_command("get_layer_features", {
        "layer_id": setup_test_data,
        "limit": 1
    })
    feature = resp["result"]["features"][0]
    assert "geometry" not in feature, "Geometry should be omitted by default"
    assert feature["attributes"]["id"] is not None

def test_geometry_inclusion(client, setup_test_data):
    """Test that geometry IS included when requested."""
    resp = client.send_command("get_layer_features", {
        "layer_id": setup_test_data,
        "limit": 1,
        "include_geometry": True
    })
    feature = resp["result"]["features"][0]
    assert "geometry" in feature, "Geometry should be present when requested"
    assert feature["geometry"]["type"] is not None

def test_large_data_buffer(client):
    """Test receiving a large framed payload."""
    resp = client.execute_code('print("X" * 100000)')
    assert resp["status"] == "success"
    assert len(resp["result"]["stdout"]) >= 100000
