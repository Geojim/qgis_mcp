import pytest
import socket
import json
import time
import uuid

# --- Client Implementation (Based on your modifications) ---
class QgisMCPClient:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.socket = None
    
    def connect(self):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(5)  # 5s connection timeout
            self.socket.connect((self.host, self.port))
            return True
        except Exception as e:
            print(f"Connection failed: {e}")
            return False
    
    def disconnect(self):
        if self.socket:
            self.socket.close()
            self.socket = None
    
    def send_command(self, command_type, params=None):
        if not self.socket:
            raise ConnectionError("Not connected to server")
        
        command = {
            "type": command_type,
            "params": params or {}
        }
        
        # Send
        self.socket.sendall(json.dumps(command).encode('utf-8'))
        
        # Receive with timeout and buffering
        self.socket.settimeout(30) # 30s read timeout (matches your server fix)
        response_data = b''
        
        while True:
            try:
                chunk = self.socket.recv(65536) # 64KB buffer (matches your server fix)
                if not chunk:
                    break
                response_data += chunk
                
                try:
                    return json.loads(response_data.decode('utf-8'))
                except json.JSONDecodeError:
                    continue # Wait for more data
            except socket.timeout:
                raise TimeoutError("Server response timed out")
                
        raise ValueError("Empty or invalid response from server")

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
    
    # Python code to create a memory layer with 5 features
    setup_code = f"""
from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY, QgsProject
import sys

print("Starting layer creation...", file=sys.stdout)
try:
    layer = QgsVectorLayer("Point?crs=epsg:4326&field=id:integer&field=name:string", "{layer_name}", "memory")
    if not layer.isValid():
        print("ERROR: Layer is invalid!", file=sys.stderr)
        raise Exception("Failed to create memory layer")
    else:
        print(f"Layer created, validity: {{layer.isValid()}}", file=sys.stdout)

    pr = layer.dataProvider()
    features = []
    for i in range(5):
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(i, i)))
        f.setAttributes([i, f"Feature {{i}}"])
        features.append(f)

    res = pr.addFeatures(features)
    print(f"Features added: {{res}}", file=sys.stdout)
    
    QgsProject.instance().addMapLayer(layer)
    print(f"Layer added to project: {{layer.id()}}", file=sys.stdout)
    
except Exception as e:
    print(f"EXCEPTION: {{e}}", file=sys.stderr)
    raise e
    """
    
    result = client.send_command("execute_code", {"code": setup_code})
    assert result.get("status") == "success", f"Failed to setup test data: {result}"
    print(f"\n[DEBUG] Layer creation stdout:\n{result.get('result', {}).get('stdout', 'NO STDOUT')}")
    print(f"[DEBUG] Layer creation error:\n{result.get('result', {}).get('stderr', 'NO STDERR')}")
    
    # Find the layer ID we just created with retry mechanism
    timeout = 5  # 5 seconds timeout
    start_time = time.time()
    target_layer = None
    
    print(f"[DEBUG] Waiting for layer '{layer_name}' to appear...")
    while time.time() - start_time < timeout:
        layers_resp = client.send_command("get_layers")
        layers = layers_resp.get("result", [])
        
        # Debug: print all available layers
        current_names = [l["name"] for l in layers]
        print(f"[DEBUG] Current layers: {current_names}")
        
        target_layer = next((l for l in layers if l["name"] == layer_name), None)
        if target_layer:
            print(f"[DEBUG] Found layer: {target_layer['id']}")
            break
        time.sleep(0.5)
            
    assert target_layer is not None, f"Test layer '{layer_name}' not found. Available layers: {current_names}"
    
    yield target_layer["id"]
    
    # Cleanup
    client.send_command("remove_layer", {"layer_id": target_layer["id"]})


# --- Tests ---

def test_ping(client):
    """Test basic connectivity."""
    resp = client.send_command("ping")
    assert resp == {"status": "success", "result": {"pong": True}}

def test_get_qgis_info(client):
    """Test retrieving system info."""
    resp = client.send_command("get_qgis_info")
    assert resp["status"] == "success"
    assert "qgis_version" in resp["result"]

def test_get_layers_basic(client, setup_test_data):
    """Test layer listing."""
    resp = client.send_command("get_layers")
    assert resp["status"] == "success"
    layers = resp["result"]
    assert len(layers) > 0
    # Verify our test layer is there
    ids = [l["id"] for l in layers]
    assert setup_test_data in ids

def test_feature_limit(client, setup_test_data):
    """Test that the 'limit' parameter works."""
    # Request 3 features (layer has 5)
    resp = client.send_command("get_layer_features", {
        "layer_id": setup_test_data, 
        "limit": 3,
        "include_geometry": False
    })
    
    assert resp["status"] == "success"
    features = resp["result"]["features"]
    assert len(features) == 3

def test_geometry_exclusion(client, setup_test_data):
    """Test that geometry is excluded by default (your optimization)."""
    resp = client.send_command("get_layer_features", {
        "layer_id": setup_test_data, 
        "limit": 1
    })
    
    feature = resp["result"]["features"][0]
    # Should NOT have 'geometry' key
    assert "geometry" not in feature, "Geometry should be omitted by default"
    # Should check attributes exist
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
    """Test receiving a large payload (simulated via execute_code)."""
    # Generate a large string in Python and return it
    large_string_code = """
data = "X" * 100000  # 100KB string
print(data)
    """
    resp = client.send_command("execute_code", {"code": large_string_code})
    assert resp["status"] == "success"
    # We check stdout length in the result
    assert len(resp["result"]["stdout"]) >= 100000
