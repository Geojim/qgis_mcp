#!/usr/bin/env python3
"""
QGIS MCP Client - Simple client to connect to the QGIS MCP server
"""

import socket
import json
import struct
import time
import argparse
import sys

class QgisMCPClient:
    def __init__(self, host='localhost', port=9876, default_timeout=30):
        self.host = host
        self.port = port
        self.socket = None
        self.default_timeout = default_timeout
        self._request_id = 0

    def connect(self):
        """Connect to the QGIS MCP server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(5)
            self.socket.connect((self.host, self.port))
            return True
        except Exception as e:
            print(f"Error connecting to server: {str(e)}")
            self.socket = None
            return False

    def disconnect(self):
        """Disconnect from the server"""
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None

    def _recv_exactly(self, n, deadline):
        """Receive exactly n bytes before the deadline, or raise."""
        data = b''
        while len(data) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("deadline exceeded")
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(min(n - len(data), 65536))
            if not chunk:
                raise ConnectionError("Connection closed while reading response")
            data += chunk
        return data

    def send_command(self, command_type, params=None, timeout=None):
        """Send a command and return its (id-matched) response.

        Wire format: 4-byte big-endian unsigned length prefix + UTF-8 JSON
        body. Each request carries an "id" echoed by the server; stale frames
        from earlier timed-out calls are discarded. Dead sockets (e.g. after
        a QGIS restart) are reconnected and the command retried once.
        """
        timeout = timeout if timeout is not None else self.default_timeout
        last_error = None

        for attempt in range(2):
            if self.socket is None and not self.connect():
                last_error = "could not connect"
                continue

            self._request_id += 1
            rid = self._request_id
            command = {"type": command_type, "params": params or {}, "id": rid}
            payload = json.dumps(command).encode('utf-8')

            try:
                self.socket.settimeout(timeout)
                self.socket.sendall(struct.pack('>I', len(payload)) + payload)
            except socket.timeout:
                self.disconnect()
                return {"status": "error",
                        "message": f"Timed out sending '{command_type}'"}
            except OSError as e:
                self.disconnect()
                last_error = str(e)
                continue

            deadline = time.monotonic() + timeout
            try:
                while True:
                    length = struct.unpack('>I', self._recv_exactly(4, deadline))[0]
                    body = self._recv_exactly(length, deadline)
                    response = json.loads(body.decode('utf-8'))
                    resp_id = response.get("id")
                    if resp_id == rid or resp_id is None:
                        return response
                    # Stale reply from an earlier timed-out call; discard.
            except socket.timeout:
                # Stream may be mid-frame: close so the next call reconnects
                # fresh instead of desyncing.
                self.disconnect()
                return {"status": "error",
                        "message": f"'{command_type}' timed out after {timeout}s"}
            except (ConnectionError, OSError) as e:
                self.disconnect()
                last_error = str(e)
                continue

        return {"status": "error",
                "message": f"Could not reach QGIS: {last_error}"}

    def ping(self):
        """Simple ping command to check server connectivity"""
        return self.send_command("ping")

    def get_qgis_info(self):
        """Get QGIS information"""
        return self.send_command("get_qgis_info")

    def get_project_info(self):
        """Get current project information"""
        return self.send_command("get_project_info")

    def execute_code(self, code, timeout=None):
        """Execute arbitrary PyQGIS code"""
        return self.send_command("execute_code", {"code": code}, timeout=timeout)

    def submit_code(self, code):
        """Submit PyQGIS code as an async job; returns a job id immediately"""
        return self.send_command("submit_code", {"code": code})

    def poll_job(self, job_id, timeout=None):
        """Poll an async job started with submit_code"""
        return self.send_command("poll_job", {"job_id": job_id}, timeout=timeout)

    def add_vector_layer(self, path, name=None, provider="ogr"):
        """Add a vector layer to the project"""
        params = {
            "path": path,
            "provider": provider
        }
        if name:
            params["name"] = name

        return self.send_command("add_vector_layer", params)

    def add_raster_layer(self, path, name=None, provider="gdal"):
        """Add a raster layer to the project"""
        params = {
            "path": path,
            "provider": provider
        }
        if name:
            params["name"] = name

        return self.send_command("add_raster_layer", params)

    def get_layers(self):
        """Get all layers in the project"""
        return self.send_command("get_layers")

    def get_layer_tree(self):
        """Get the nested layer tree (groups + layers)"""
        return self.send_command("get_layer_tree")

    def set_node_visibility(self, visible, layer_id=None, group_path=None,
                            check_ancestors=True):
        """Check/uncheck a layer or group node in the layer tree"""
        params = {"visible": visible, "check_ancestors": check_ancestors}
        if layer_id:
            params["layer_id"] = layer_id
        if group_path:
            params["group_path"] = group_path
        return self.send_command("set_node_visibility", params)

    def move_node(self, layer_id=None, group_path=None, target_group_path=None,
                  index=-1):
        """Move a layer or group into another group (or the root)"""
        params = {"index": index}
        if layer_id:
            params["layer_id"] = layer_id
        if group_path:
            params["group_path"] = group_path
        if target_group_path:
            params["target_group_path"] = target_group_path
        return self.send_command("move_node", params)

    def add_group(self, name, parent_path=None, index=-1):
        """Create a layer-tree group"""
        params = {"name": name, "index": index}
        if parent_path:
            params["parent_path"] = parent_path
        return self.send_command("add_group", params)

    def get_extent(self):
        """Get the current canvas extent"""
        return self.send_command("get_extent")

    def set_extent(self, xmin, ymin, xmax, ymax, refresh=True):
        """Set the canvas extent (zoom) to the given bbox in project CRS"""
        return self.send_command("set_extent", {
            "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
            "refresh": refresh})

    def remove_layer(self, layer_id):
        """Remove a layer from the project"""
        return self.send_command("remove_layer", {"layer_id": layer_id})

    def zoom_to_layer(self, layer_id):
        """Zoom to a layer's extent"""
        return self.send_command("zoom_to_layer", {"layer_id": layer_id})

    def get_layer_features(self, layer_id, limit=10):
        """Get features from a vector layer"""
        return self.send_command("get_layer_features", {"layer_id": layer_id, "limit": limit})

    def execute_processing(self, algorithm, parameters, timeout=None):
        """Execute a processing algorithm"""
        return self.send_command("execute_processing", {
            "algorithm": algorithm,
            "parameters": parameters
        }, timeout=timeout)

    def save_project(self, path=None):
        """Save the current project"""
        params = {}
        if path:
            params["path"] = path

        return self.send_command("save_project", params)

    def load_project(self, path, timeout=None):
        """Load a project"""
        return self.send_command("load_project", {"path": path}, timeout=timeout)

    def render_map(self, path, width=800, height=600, timeout=None):
        """Render the current map view to an image"""
        return self.send_command("render_map", {
            "path": path,
            "width": width,
            "height": height
        }, timeout=timeout)


def print_json(data):
    """Print formatted JSON data"""
    print(json.dumps(data, indent=2))

def main():
    parser = argparse.ArgumentParser(description="Quick smoke test against the QGIS MCP plugin")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9876)
    args = parser.parse_args()

    client = QgisMCPClient(host=args.host, port=args.port)
    if not client.connect():
        print("Could not connect to the QGIS MCP server")
        sys.exit(1)

    try:
        print("Checking connection...")
        response = client.ping()
        if response and response.get("status") == "success":
            print("Connected")
        else:
            print("Connection error")
            sys.exit(1)

        print("\nQGIS info:")
        print_json(client.get_qgis_info())

        print("\nProject info:")
        print_json(client.get_project_info())

        print("\nLayer tree:")
        print_json(client.get_layer_tree())

        print("\nCanvas extent:")
        print_json(client.get_extent())
    finally:
        client.disconnect()

if __name__ == "__main__":
    main()
