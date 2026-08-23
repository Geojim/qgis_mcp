#!/usr/bin/env python3
"""
QGIS MCP Client - Simple client to connect to the QGIS MCP server
"""

import os
import logging
import time
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
    """Socket client to the QGIS plugin.

    Wire format: 4-byte big-endian unsigned length prefix + UTF-8 JSON body.
    Every request carries a monotonically increasing "id" which the plugin
    echoes back, so replies are correlated with requests and stale frames
    (left over from calls that timed out client-side) are discarded instead
    of being returned as the next call's result.
    """

    def __init__(self, host='localhost', port=9876, default_timeout=None):
        self.host = host
        self.port = port
        self.socket = None
        self._request_id = 0
        self.default_timeout = default_timeout if default_timeout is not None \
            else float(os.getenv("QGIS_MCP_TIMEOUT", "30"))

    def connect(self):
        """Connect to the QGIS plugin socket server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(5)
            self.socket.connect((self.host, self.port))
            return True
        except Exception as e:
            logger.warning(f"Error connecting to QGIS: {e}")
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

    def _ensure_connected(self):
        if self.socket is None and not self.connect():
            raise ConnectionError(
                f"Could not connect to QGIS on {self.host}:{self.port}. "
                "Make sure the QGIS MCP plugin server is running.")

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

        On a dead/stale socket (e.g. QGIS was restarted) the connection is
        re-established and the command retried once, transparently. On a
        timeout the socket is closed — the stream may be mid-frame and any
        late reply must not leak into the next call — and the next call
        reconnects fresh.
        """
        timeout = timeout if timeout is not None else self.default_timeout
        last_error = None

        for attempt in range(2):
            try:
                self._ensure_connected()
            except ConnectionError as e:
                last_error = str(e)
                continue

            self._request_id += 1
            rid = self._request_id
            command = {"type": command_type, "params": params or {}, "id": rid}
            payload = json.dumps(command).encode('utf-8')

            # Send. A connection error here means a stale socket (QGIS
            # restarted, WinError 10053 etc.) — reconnect and retry once.
            try:
                self.socket.settimeout(timeout)
                self.socket.sendall(struct.pack('>I', len(payload)) + payload)
            except socket.timeout:
                self.disconnect()
                return {"status": "error",
                        "message": f"Timed out sending '{command_type}' after {timeout}s"}
            except OSError as e:
                logger.warning(f"Send failed ({e}); reconnecting")
                self.disconnect()
                last_error = str(e)
                continue

            # Receive frames until we see our request id; discard stale ones.
            deadline = time.monotonic() + timeout
            try:
                while True:
                    length = struct.unpack('>I', self._recv_exactly(4, deadline))[0]
                    body = self._recv_exactly(length, deadline)
                    try:
                        response = json.loads(body.decode('utf-8'))
                    except json.JSONDecodeError as e:
                        # Framing guarantees message boundaries, so this is a
                        # server-side bug, not a desync; surface it.
                        return {"status": "error",
                                "message": f"Invalid JSON response: {e}"}
                    resp_id = response.get("id")
                    if resp_id == rid:
                        return response
                    if resp_id is None:
                        # Plugin predating id-echo: accept FIFO, best effort.
                        logger.warning(
                            "Response carries no request id — QGIS plugin is "
                            "outdated; update/reload it for desync protection")
                        return response
                    logger.warning(
                        f"Discarding stale reply id={resp_id} (awaiting {rid}) "
                        f"for '{command_type}'")
            except socket.timeout:
                # Stream may be mid-frame; poison it so the next call starts
                # on a fresh connection instead of desyncing.
                self.disconnect()
                return {
                    "status": "error",
                    "message": (
                        f"'{command_type}' timed out after {timeout}s waiting for a "
                        "response. QGIS may still be executing it. For long "
                        "operations pass a larger timeout, or use "
                        "submit_code/poll_job."),
                }
            except (ConnectionError, OSError) as e:
                logger.warning(f"Receive failed ({e}); reconnecting")
                self.disconnect()
                last_error = str(e)
                continue

        return {"status": "error",
                "message": f"Could not reach QGIS: {last_error}"}


_qgis_connection = None


def get_qgis_connection():
    """Get the persistent QGIS connection (created lazily; self-healing)."""
    global _qgis_connection
    if _qgis_connection is None:
        _qgis_connection = QgisMCPServer(
            host=os.getenv("QGIS_MCP_HOST", "localhost"),
            port=int(os.getenv("QGIS_MCP_PORT", "9876")),
        )
        # Connection is established lazily by send_command, which also
        # detects dead sockets and reconnects transparently mid-call.
    return _qgis_connection


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("QgisMCPServer server starting up")

        # Try to connect to Qgis on startup to verify it's available
        qgis = get_qgis_connection()
        if qgis.connect():
            logger.info("Successfully connected to Qgis on startup")
        else:
            logger.warning(
                "Could not connect to Qgis on startup. Make sure the Qgis "
                "plugin is running — the connection will be retried on first use.")

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
def load_project(ctx: Context, path: str, timeout: float = None) -> str:
    """Load a QGIS project from the specified path.

    Args:
        timeout: optional per-call timeout in seconds (default 30) — raise it
                 for heavy projects that take long to open.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("load_project", {"path": path}, timeout=timeout)
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
def execute_processing(ctx: Context, algorithm: str, parameters: dict,
                       timeout: float = None) -> str:
    """Execute a processing algorithm with the given parameters.

    Args:
        timeout: optional per-call timeout in seconds (default 30) — raise it
                 for slow algorithms.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("execute_processing", {
                               "algorithm": algorithm, "parameters": parameters},
                               timeout=timeout)
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
               layer_ids: list = None, extent: list = None,
               timeout: float = None) -> str:
    """Render the map to an image file, off-screen and single-threaded (crash-safe).

    Safe to use on any project, including ECW raster imagery and live
    MSSQL/ODBC layers. (A historical hard-crash via the ECW driver mutex only
    affected the old MULTI-threaded renderer; the current single-threaded
    implementation was verified live on an ECW+mssql project, 2026-08-23.)
    Pass layer_ids to render a chosen subset — useful for speed or isolating
    styling. The result includes render_seconds (render duration, excluding
    image save) for judging provider/styling performance.

    Args:
        path:      output image path.
        width/height: output size in pixels.
        layer_ids: optional list of layer IDs to render ONLY those layers. When
                   omitted, falls back to currently checked layers (can be heavy).
        extent:    optional [xmin, ymin, xmax, ymax] in project CRS; defaults to
                   the current canvas extent.
        timeout:   optional per-call timeout in seconds (default 30).
    """
    qgis = get_qgis_connection()
    params = {"path": path, "width": width, "height": height}
    if layer_ids is not None:
        params["layer_ids"] = layer_ids
    if extent is not None:
        params["extent"] = extent
    result = qgis.send_command("render_map", params, timeout=timeout)
    return json.dumps(result, indent=2)


@mcp.tool()
def execute_code(ctx: Context, code: str, timeout: float = None) -> str:
    """Execute arbitrary PyQGIS code provided as a string.

    Args:
        timeout: optional per-call timeout in seconds (default 30). For code
                 that may run longer, either raise this or use
                 submit_code/poll_job so the call can't time out mid-flight.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("execute_code", {"code": code}, timeout=timeout)
    return json.dumps(result, indent=2)


@mcp.tool()
def submit_code(ctx: Context, code: str) -> str:
    """Submit PyQGIS code as an async job; returns a job_id immediately.

    Use for long-running code (bulk layer creation, big exports) instead of
    execute_code, so the client's socket timeout never races the execution.
    Check progress/result with poll_job.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("submit_code", {"code": code})
    return json.dumps(result, indent=2)


@mcp.tool()
def poll_job(ctx: Context, job_id: str, timeout: float = None) -> str:
    """Poll an async job started with submit_code.

    Returns status pending/running/done/error, plus the execute_code-style
    result once finished. Note: while the job is executing, QGIS's main
    thread is busy and the poll reply arrives only after the job completes —
    pass a generous timeout for jobs known to run long.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("poll_job", {"job_id": job_id}, timeout=timeout)
    return json.dumps(result, indent=2)


@mcp.tool()
def get_layer_tree(ctx: Context) -> str:
    """Get the nested layer tree: groups and layers with checked/visible/expanded
    state. Unlike get_layers (flat), this preserves group structure."""
    qgis = get_qgis_connection()
    result = qgis.send_command("get_layer_tree")
    return json.dumps(result, indent=2)


@mcp.tool()
def set_node_visibility(ctx: Context, visible: bool, layer_id: str = None,
                        group_path: str = None, check_ancestors: bool = True) -> str:
    """Check/uncheck a layer or group node in the layer tree.

    Identify the node by layer_id OR group_path ('/'-separated, e.g.
    'Seams/GM'). When enabling with check_ancestors=True (default), ancestor
    groups are checked too so the node actually becomes visible.
    """
    qgis = get_qgis_connection()
    params = {"visible": visible, "check_ancestors": check_ancestors}
    if layer_id:
        params["layer_id"] = layer_id
    if group_path:
        params["group_path"] = group_path
    result = qgis.send_command("set_node_visibility", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def move_node(ctx: Context, layer_id: str = None, group_path: str = None,
              target_group_path: str = None, index: int = -1) -> str:
    """Move a layer (layer_id) or group (group_path) into another group.

    target_group_path=None moves to the root; index=-1 appends.
    """
    qgis = get_qgis_connection()
    params = {"index": index}
    if layer_id:
        params["layer_id"] = layer_id
    if group_path:
        params["group_path"] = group_path
    if target_group_path:
        params["target_group_path"] = target_group_path
    result = qgis.send_command("move_node", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def add_group(ctx: Context, name: str, parent_path: str = None, index: int = -1) -> str:
    """Create a layer-tree group under parent_path (root when omitted)."""
    qgis = get_qgis_connection()
    params = {"name": name, "index": index}
    if parent_path:
        params["parent_path"] = parent_path
    result = qgis.send_command("add_group", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def get_extent(ctx: Context) -> str:
    """Get the current canvas extent (bbox in project CRS, scale, canvas size)."""
    qgis = get_qgis_connection()
    result = qgis.send_command("get_extent")
    return json.dumps(result, indent=2)


@mcp.tool()
def set_extent(ctx: Context, xmin: float, ymin: float, xmax: float, ymax: float,
               refresh: bool = True) -> str:
    """Set the canvas extent (zoom) to the given bbox in project CRS.

    refresh=True (default) repaints the canvas. A bridge-triggered refresh
    was historically a crash trigger on ECW+ODBC projects but survived live
    verification on one (2026-08-23); pass refresh=False as a precaution if
    a specific project proves unstable.
    """
    qgis = get_qgis_connection()
    result = qgis.send_command("set_extent", {
        "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
        "refresh": refresh})
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
