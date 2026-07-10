#!/usr/bin/env python3
"""
QGIS MCP Client - Simple client to connect to the QGIS MCP server
"""

import os
import logging
from contextlib import asynccontextmanager
import socket
import struct
import json
from typing import AsyncIterator, Dict, Any
from mcp.server.fastmcp import FastMCP, Context

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QgisMCPServer")


class QgisMCPServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.socket = None

    def connect(self):
        """Connect to the QGIS MCP server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.host, self.port))
            return True
        except Exception as e:
            print(f"Error connecting to server: {str(e)}")
            return False

    def disconnect(self):
        """Disconnect from the server"""
        if self.socket:
            self.socket.close()
            self.socket = None

    def _recv_exactly(self, n):
        """Receive exactly n bytes from the socket, or raise on EOF."""
        data = b''
        while len(data) < n:
            chunk = self.socket.recv(min(n - len(data), 65536))
            if not chunk:
                raise ConnectionError("Connection closed while reading response")
            data += chunk
        return data

    def send_command(self, command_type, params=None):
        """Send a command to the server and get the response.

        Wire format: 4-byte big-endian unsigned length prefix + UTF-8 JSON body.
        """
        if not self.socket:
            print("Not connected to server")
            return None

        # Create command
        command = {
            "type": command_type,
            "params": params or {}
        }

        try:
            # Set a receive timeout to avoid infinite waiting.
            self.socket.settimeout(30)  # 30 seconds timeout

            # Send the command, length-prefixed
            payload = json.dumps(command).encode('utf-8')
            self.socket.sendall(struct.pack('>I', len(payload)) + payload)

            # Receive the response: read the 4-byte length, then the body
            length = struct.unpack('>I', self._recv_exactly(4))[0]
            response_data = self._recv_exactly(length)

            # Restore to no timeout
            self.socket.settimeout(None)

            # Parse and return the response
            if not response_data:
                return {"status": "error", "message": "Empty response from server"}

            try:
                return json.loads(response_data.decode('utf-8'))
            except json.JSONDecodeError as e:
                return {"status": "error", "message": f"Invalid JSON response: {str(e)}"}

        except socket.timeout:
            print(f"Socket operation timed out after 30 seconds")
            return {"status": "error", "message": "Connection timed out"}
        except Exception as e:
            print(f"Error sending command: {str(e)}")
            return {"status": "error", "message": str(e)}


_qgis_connection = None


def get_qgis_connection():
    """Get or create a persistent Qgis connection"""
    global _qgis_connection

    # If we have an existing connection, check if it's still valid
    if _qgis_connection is not None:
        # Test if the connection is still alive with a simple ping
        try:
            # Just try to send a small message to check if the socket is still connected
            _qgis_connection.socket.sendall(b'')
            return _qgis_connection
        except Exception as e:
            # Connection is dead, close it and create a new one
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _qgis_connection.disconnect()
            except Exception:
                pass
            _qgis_connection = None

    # Create a new connection if needed
    if _qgis_connection is None:
        _qgis_connection = QgisMCPServer(
            host=os.getenv("QGIS_MCP_HOST", "localhost"),
            port=int(os.getenv("QGIS_MCP_PORT", "9876")),
        )
        if not _qgis_connection.connect():
            logger.error("Failed to connect to Qgis")
            _qgis_connection = None
            raise Exception(
                "Could not connect to Qgis. Make sure the Qgis plugin is running.")
        logger.info("Created new persistent connection to Qgis")

    return _qgis_connection


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("QgisMCPServer server starting up")

        # Try to connect to Qgis on startup to verify it's available
        try:
            qgis = get_qgis_connection()
            logger.info("Successfully connected to Qgis on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Qgis on startup: {str(e)}")
            logger.warning(
                "Make sure the Qgis addon is running before using Qgis resources or tools")

        yield {}
    finally:
        global _qgis_connection
        if _qgis_connection:
            logger.info("Disconnecting from Qgis on shutdown")
            _qgis_connection.disconnect()
            _qgis_connection = None
        logger.info("QgisMCPServer server shut down")

mcp = FastMCP(
    name="Qgis_mcp",
    instructions="Qgis integration through the Model Context Protocol",
    lifespan=server_lifespan
)


@mcp.tool()
def ping(ctx: Context) -> str:
    """Simple ping command to check server connectivity"""
    qgis = get_qgis_connection()
    result = qgis.send_command("ping")
    return json.dumps(result, indent=2)


@mcp.tool()
def get_qgis_info(ctx: Context) -> str:
    """Get QGIS information"""
    qgis = get_qgis_connection()
    result = qgis.send_command("get_qgis_info")
    return json.dumps(result, indent=2)


@mcp.tool()
def load_project(ctx: Context, path: str) -> str:
    """Load a QGIS project from the specified path."""
    qgis = get_qgis_connection()
    result = qgis.send_command("load_project", {"path": path})
    return json.dumps(result, indent=2)


@mcp.tool()
def create_new_project(ctx: Context, path: str) -> str:
    """Create a new project a save it"""
    qgis = get_qgis_connection()
    result = qgis.send_command("create_new_project", {"path": path})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_project_info(ctx: Context) -> str:
    """Get current project information"""
    qgis = get_qgis_connection()
    result = qgis.send_command("get_project_info")
    return json.dumps(result, indent=2)


@mcp.tool()
def add_vector_layer(ctx: Context, path: str, provider: str = "ogr", name: str = None) -> str:
    """Add a vector layer to the project."""
    qgis = get_qgis_connection()
    params = {"path": path, "provider": provider}
    if name:
        params["name"] = name
    result = qgis.send_command("add_vector_layer", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def add_raster_layer(ctx: Context, path: str, provider: str = "gdal", name: str = None) -> str:
    """Add a raster layer to the project."""
    qgis = get_qgis_connection()
    params = {"path": path, "provider": provider}
    if name:
        params["name"] = name
    result = qgis.send_command("add_raster_layer", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def get_layers(ctx: Context) -> str:
    """Retrieve all layers in the current project."""
    qgis = get_qgis_connection()
    result = qgis.send_command("get_layers")
    return json.dumps(result, indent=2)


@mcp.tool()
def remove_layer(ctx: Context, layer_id: str) -> str:
    """Remove a layer from the project by its ID."""
    qgis = get_qgis_connection()
    result = qgis.send_command("remove_layer", {"layer_id": layer_id})
    return json.dumps(result, indent=2)


@mcp.tool()
def zoom_to_layer(ctx: Context, layer_id: str) -> str:
    """Zoom to the extent of a specified layer."""
    qgis = get_qgis_connection()
    result = qgis.send_command("zoom_to_layer", {"layer_id": layer_id})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_layer_features(ctx: Context, layer_id: str, limit: int = 10, include_geometry: bool = False) -> str:
    """Retrieve features from a vector layer with an optional limit."""
    qgis = get_qgis_connection()
    result = qgis.send_command("get_layer_features", {
                               "layer_id": layer_id, "limit": limit, "include_geometry": include_geometry})
    return json.dumps(result, indent=2)


@mcp.tool()
def execute_processing(ctx: Context, algorithm: str, parameters: dict) -> str:
    """Execute a processing algorithm with the given parameters."""
    qgis = get_qgis_connection()
    result = qgis.send_command("execute_processing", {
                               "algorithm": algorithm, "parameters": parameters})
    return json.dumps(result, indent=2)


@mcp.tool()
def save_project(ctx: Context, path: str = None) -> str:
    """Save the current project to the given path, or to the current project path if not specified."""
    qgis = get_qgis_connection()
    params = {}
    if path:
        params["path"] = path
    result = qgis.send_command("save_project", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def render_map(ctx: Context, path: str, width: int = 800, height: int = 600,
               layer_ids: list = None, extent: list = None) -> str:
    """Render the map to an image file, off-screen and single-threaded (crash-safe).

    On heavy projects (ECW raster imagery + live MSSQL/ODBC layers) rendering ALL
    checked layers can hard-crash QGIS via the ECW driver mutex. Pass layer_ids to
    render only a chosen subset (e.g. vector layers only, excluding ECW/ODBC).

    Args:
        path:      output image path.
        width/height: output size in pixels.
        layer_ids: optional list of layer IDs to render ONLY those layers. When
                   omitted, falls back to currently checked layers (can be heavy).
        extent:    optional [xmin, ymin, xmax, ymax] in project CRS; defaults to
                   the current canvas extent.
    """
    qgis = get_qgis_connection()
    params = {"path": path, "width": width, "height": height}
    if layer_ids is not None:
        params["layer_ids"] = layer_ids
    if extent is not None:
        params["extent"] = extent
    result = qgis.send_command("render_map", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def execute_code(ctx: Context, code: str) -> str:
    """Execute arbitrary PyQGIS code provided as a string."""
    qgis = get_qgis_connection()
    result = qgis.send_command("execute_code", {"code": code})
    return json.dumps(result, indent=2)


@mcp.tool()
def set_layer_visibility(ctx: Context, layer_id: str, visible: bool) -> str:
    """Toggle a layer's visibility on or off."""
    qgis = get_qgis_connection()
    result = qgis.send_command("set_layer_visibility", {"layer_id": layer_id, "visible": visible})
    return json.dumps(result, indent=2)


@mcp.tool()
def filter_layer(ctx: Context, layer_id: str, expression: str = "") -> str:
    """Set a subset filter (SQL WHERE clause) on a vector layer. Pass empty string to clear."""
    qgis = get_qgis_connection()
    result = qgis.send_command("filter_layer", {"layer_id": layer_id, "expression": expression})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_layer_fields(ctx: Context, layer_id: str) -> str:
    """Get field metadata (name, type, length, precision) for a vector layer."""
    qgis = get_qgis_connection()
    result = qgis.send_command("get_layer_fields", {"layer_id": layer_id})
    return json.dumps(result, indent=2)


@mcp.tool()
def group_layers(ctx: Context, name: str, layer_ids: list[str]) -> str:
    """Create a layer group and move the specified layers into it."""
    qgis = get_qgis_connection()
    result = qgis.send_command("group_layers", {"name": name, "layer_ids": layer_ids})
    return json.dumps(result, indent=2)


@mcp.tool()
def select_features(ctx: Context, layer_id: str, expression: str = "", mode: str = "set") -> str:
    """Select features by expression, or clear selection if expression is empty."""
    qgis = get_qgis_connection()
    result = qgis.send_command("select_features", {"layer_id": layer_id, "expression": expression, "mode": mode})
    return json.dumps(result, indent=2)


if __name__ == "__main__":
    mcp.run()
