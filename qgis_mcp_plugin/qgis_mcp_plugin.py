import os
import io
import sys
import json
import socket
import struct
import time
import traceback
from qgis.core import *
from qgis.gui import *
from qgis.PyQt.QtCore import QObject, pyqtSignal, QTimer, Qt, QSize
try:
    from qgis.PyQt.QtCore import QVariant
    HAS_QVARIANT = True
except ImportError:
    HAS_QVARIANT = False
from qgis.PyQt.QtWidgets import QAction, QDockWidget, QVBoxLayout, QLabel, QPushButton, QSpinBox, QWidget
from qgis.PyQt.QtGui import QIcon, QColor, QImage, QPainter
from qgis.utils import active_plugins

# Qt5/Qt6 compatible enum values
try:
    _RightDockWidgetArea = Qt.DockWidgetArea.RightDockWidgetArea  # Qt6
except AttributeError:
    _RightDockWidgetArea = Qt.RightDockWidgetArea  # Qt5

# QGIS 3.30+ scoped enums with fallback for older versions
try:
    _VectorLayerType = Qgis.LayerType.Vector
    _RasterLayerType = Qgis.LayerType.Raster
except AttributeError:
    _VectorLayerType = QgsMapLayer.VectorLayer
    _RasterLayerType = QgsMapLayer.RasterLayer


def _find_group_by_path(path):
    """Resolve a '/'-separated group path (e.g. 'Seams/GM') to its tree node.

    Path-based (not findGroup by bare name) so same-named groups under
    different parents resolve unambiguously. Returns None if not found.
    """
    node = QgsProject.instance().layerTreeRoot()
    for part in [p for p in str(path).split("/") if p]:
        node = next(
            (c for c in node.children()
             if isinstance(c, QgsLayerTreeGroup) and c.name() == part),
            None)
        if node is None:
            return None
    return node


def _iter_group_paths(node=None, prefix=""):
    """Yield (path, tree_node) for every group in the layer tree, depth-first."""
    if node is None:
        node = QgsProject.instance().layerTreeRoot()
    for child in node.children():
        if isinstance(child, QgsLayerTreeGroup):
            path = f"{prefix}/{child.name()}" if prefix else child.name()
            yield path, child
            yield from _iter_group_paths(child, path)


def _check_with_ancestors(node):
    """Check a tree node's visibility box, and its ancestor groups' boxes too,
    so the node actually becomes visible on the canvas."""
    node.setItemVisibilityChecked(True)
    parent = node.parent()
    while parent is not None and parent.parent() is not None:  # stop at root
        parent.setItemVisibilityChecked(True)
        parent = parent.parent()


class QgisMCPServer(QObject):
    """Server class to handle socket connections and execute QGIS commands"""

    def __init__(self, host='localhost', port=9876, iface=None):
        super().__init__()
        self.host = host
        self.port = port
        self.iface = iface
        self.running = False
        self.socket = None
        self.client = None
        self.buffer = b''
        self.timer = None
        # Async job store for submit_code/poll_job. Completed jobs are kept
        # until pruned so a client that timed out can still fetch the result.
        self.jobs = {}
        self._job_counter = 0

    def start(self):
        """Start the server"""
        self.running = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            self.socket.setblocking(False)

            # Create a timer to process server operations
            self.timer = QTimer()
            self.timer.timeout.connect(self.process_server)
            self.timer.start(100)  # 100ms interval

            QgsMessageLog.logMessage(
                f"QGIS MCP server started on {self.host}:{self.port}", "QGIS MCP")
            return True
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Failed to start server: {str(e)}", "QGIS MCP", Qgis.Critical)
            self.stop()
            return False

    def stop(self):
        """Stop the server"""
        self.running = False

        if self.timer:
            self.timer.stop()
            self.timer = None

        if self.socket:
            self.socket.close()
        if self.client:
            self.client.close()

        self.socket = None
        self.client = None
        QgsMessageLog.logMessage("QGIS MCP server stopped", "QGIS MCP")

    def process_server(self):
        """Process server operations (called by timer)"""
        if not self.running:
            return

        try:
            # Accept new connections
            if not self.client and self.socket:
                try:
                    self.client, address = self.socket.accept()
                    self.client.setblocking(False)
                    QgsMessageLog.logMessage(
                        f"Connected to client: {address}", "QGIS MCP")
                except BlockingIOError:
                    pass  # No connection waiting
                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"Error accepting connection: {str(e)}", "QGIS MCP", Qgis.Warning)

            # Process existing connection
            if self.client:
                try:
                    # Drain all data currently available on the non-blocking
                    # socket into the buffer. Wire format: 4-byte big-endian
                    # unsigned length prefix + UTF-8 JSON body.
                    disconnected = False
                    try:
                        while True:
                            chunk = self.client.recv(8192)
                            if not chunk:
                                disconnected = True
                                break
                            self.buffer += chunk
                    except BlockingIOError:
                        pass  # No more data available right now

                    if disconnected:
                        # Connection closed by client
                        QgsMessageLog.logMessage(
                            "Client disconnected", "QGIS MCP")
                        self.client.close()
                        self.client = None
                        self.buffer = b''
                        return

                    # Process every complete framed message in the buffer
                    while len(self.buffer) >= 4:
                        msg_len = struct.unpack('>I', self.buffer[:4])[0]
                        if len(self.buffer) < 4 + msg_len:
                            break  # Incomplete message, wait for more data
                        message = self.buffer[4:4 + msg_len]
                        self.buffer = self.buffer[4 + msg_len:]
                        command = None
                        try:
                            command = json.loads(message.decode('utf-8'))
                            response = self.execute_command(command)
                        except Exception as e:
                            QgsMessageLog.logMessage(
                                f"Error processing command: {str(e)}", "QGIS MCP", Qgis.Warning)
                            response = {"status": "error", "message": str(e)}
                        # Echo the request id so the client can correlate
                        # replies with requests and discard stale frames left
                        # over from calls that timed out client-side.
                        if isinstance(command, dict) and command.get("id") is not None:
                            response["id"] = command["id"]
                        response_json = json.dumps(response).encode('utf-8')
                        self.client.sendall(
                            struct.pack('>I', len(response_json)) + response_json)

                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"Error with client: {str(e)}", "QGIS MCP", Qgis.Warning)
                    if self.client:
                        self.client.close()
                        self.client = None
                    self.buffer = b''

        except Exception as e:
            QgsMessageLog.logMessage(
                f"Server error: {str(e)}", "QGIS MCP", Qgis.Critical)

    def execute_command(self, command):
        """Execute a command"""
        try:
            cmd_type = command.get("type")
            params = command.get("params", {})

            handlers = {
                "ping": self.ping,
                "get_qgis_info": self.get_qgis_info,
                "load_project": self.load_project,
                "get_project_info": self.get_project_info,
                "execute_code": self.execute_code,
                "add_vector_layer": self.add_vector_layer,
                "add_raster_layer": self.add_raster_layer,
                "get_layers": self.get_layers,
                "remove_layer": self.remove_layer,
                "zoom_to_layer": self.zoom_to_layer,
                "get_layer_features": self.get_layer_features,
                "execute_processing": self.execute_processing,
                "save_project": self.save_project,
                "render_map": self.render_map,
                "create_new_project": self.create_new_project,
                "set_layer_visibility": self.set_layer_visibility,
                "filter_layer": self.filter_layer,
                "get_layer_fields": self.get_layer_fields,
                "group_layers": self.group_layers,
                "select_features": self.select_features,
                "submit_code": self.submit_code,
                "poll_job": self.poll_job,
                "get_layer_tree": self.get_layer_tree,
                "set_node_visibility": self.set_node_visibility,
                "move_node": self.move_node,
                "add_group": self.add_group,
                "get_extent": self.get_extent,
                "set_extent": self.set_extent,
            }

            handler = handlers.get(cmd_type)
            if handler:
                try:
                    QgsMessageLog.logMessage(
                        f"Executing handler for {cmd_type}", "QGIS MCP")
                    result = handler(**params)
                    QgsMessageLog.logMessage(
                        f"Handler execution complete", "QGIS MCP")
                    return {"status": "success", "result": result}
                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"Error in handler: {str(e)}", "QGIS MCP", Qgis.Critical)
                    traceback.print_exc()
                    return {"status": "error", "message": str(e)}
            else:
                return {"status": "error", "message": f"Unknown command type: {cmd_type}"}

        except Exception as e:
            QgsMessageLog.logMessage(
                f"Error executing command: {str(e)}", "QGIS MCP", Qgis.Critical)
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    # --- Helpers ---

    def _is_layer_visible(self, layer_id):
        """Check if a layer is visible in the layer tree (Qt5/Qt6 safe)."""
        node = QgsProject.instance().layerTreeRoot().findLayer(layer_id)
        if node is None:
            return False
        return node.isVisible()

    def _get_layer(self, layer_id):
        """Look up any layer by ID, raise if not found."""
        project = QgsProject.instance()
        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")
        return project.mapLayer(layer_id)

    def _get_vector_layer(self, layer_id):
        """Look up a vector layer by ID, raise if not found or wrong type."""
        layer = self._get_layer(layer_id)
        if layer.type() != _VectorLayerType:
            raise Exception(f"Layer is not a vector layer: {layer_id}")
        if not layer.isValid():
            raise Exception(f"Layer data source is unavailable: {layer_id} ({layer.name()})")
        return layer

    def _get_layer_type(self, layer):
        """Helper to get layer type as string"""
        if layer.type() == _VectorLayerType:
            return f"vector_{layer.geometryType()}"
        elif layer.type() == _RasterLayerType:
            return "raster"
        else:
            return str(layer.type())

    @staticmethod
    def _convert_to_python_type(value):
        """Convert a value to a JSON-serializable Python type.

        Handles QVariant (Qt5) and native Python types (Qt6) transparently.
        """
        if HAS_QVARIANT and isinstance(value, QVariant):
            if value.isNull():
                return None
            value = value.value()
        if value is None:
            return None
        if isinstance(value, (int, float, str, bool)):
            return value
        if hasattr(value, 'toPyDate'):  # QDate
            return value.toPyDate().isoformat()
        if hasattr(value, 'toPyDateTime'):  # QDateTime
            return value.toPyDateTime().isoformat()
        try:
            return str(value)
        except Exception:
            return None

    # --- Command handlers ---

    def ping(self, **kwargs):
        """Simple ping command"""
        return {"pong": True}

    def get_qgis_info(self, **kwargs):
        """Get basic QGIS information"""
        return {
            "qgis_version": Qgis.version(),
            "profile_folder": QgsApplication.qgisSettingsDirPath(),
            "plugins_count": len(active_plugins)
        }

    def get_project_info(self, **kwargs):
        """Get information about the current QGIS project"""
        project = QgsProject.instance()

        # Get basic project information
        info = {
            "filename": project.fileName(),
            "title": project.title(),
            "layer_count": len(project.mapLayers()),
            "crs": project.crs().authid(),
            "layers": []
        }

        # Add basic layer information (limit to 10 layers for performance)
        layers = list(project.mapLayers().values())
        for i, layer in enumerate(layers):
            if i >= 10:  # Limit to 10 layers
                break

            layer_info = {
                "id": layer.id(),
                "name": layer.name(),
                "type": self._get_layer_type(layer),
                "visible": layer.isValid() and self._is_layer_visible(layer.id())
            }
            info["layers"].append(layer_info)

        return info

    def execute_code(self, code, **kwargs):
        """Execute arbitrary PyQGIS code"""

        # Capture stdout and stderr
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()

        # Store original stdout and stderr
        original_stdout = sys.stdout
        original_stderr = sys.stderr

        try:
            # Redirect stdout and stderr
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture

            # Create a local namespace for execution
            namespace = {
                "qgis": Qgis,
                "QgsProject": QgsProject,
                "iface": self.iface,
                "QgsApplication": QgsApplication,
                "QgsVectorLayer": QgsVectorLayer,
                "QgsRasterLayer": QgsRasterLayer,
                "QgsCoordinateReferenceSystem": QgsCoordinateReferenceSystem
            }

            # Execute the code
            exec(code, namespace)

            # Restore stdout and stderr
            sys.stdout = original_stdout
            sys.stderr = original_stderr

            return {
                "executed": True,
                "stdout": stdout_capture.getvalue(),
                "stderr": stderr_capture.getvalue()
            }
        except Exception as e:
            # Generate full traceback
            error_traceback = traceback.format_exc()

            # Restore stdout and stderr in case of exception
            sys.stdout = original_stdout
            sys.stderr = original_stderr

            return {
                "executed": False,
                "error": str(e),
                "traceback": error_traceback,
                "stdout": stdout_capture.getvalue(),
                "stderr": stderr_capture.getvalue()
            }

    def add_vector_layer(self, path, name=None, provider="ogr", **kwargs):
        """Add a vector layer to the project"""
        if not name:
            name = os.path.basename(path)

        # Create the layer
        layer = QgsVectorLayer(path, name, provider)

        if not layer.isValid():
            raise Exception(f"Layer is not valid: {path}")

        # Add to project
        QgsProject.instance().addMapLayer(layer)

        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
            "feature_count": layer.featureCount()
        }

    def add_raster_layer(self, path, name=None, provider="gdal", **kwargs):
        """Add a raster layer to the project"""
        if not name:
            name = os.path.basename(path)

        # Create the layer
        layer = QgsRasterLayer(path, name, provider)

        if not layer.isValid():
            raise Exception(f"Layer is not valid: {path}")

        # Add to project
        QgsProject.instance().addMapLayer(layer)

        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": "raster",
            "width": layer.width(),
            "height": layer.height()
        }

    def get_layers(self, **kwargs):
        """Get all layers in the project"""
        project = QgsProject.instance()
        layers = []

        for layer_id, layer in project.mapLayers().items():
            layer_info = {
                "id": layer_id,
                "name": layer.name(),
                "type": self._get_layer_type(layer),
                "visible": self._is_layer_visible(layer_id)
            }

            # Add type-specific information (skip expensive calls on invalid layers)
            if not layer.isValid():
                layer_info["valid"] = False
            elif layer.type() == _VectorLayerType:
                layer_info.update({
                    "feature_count": layer.featureCount(),
                    "geometry_type": layer.geometryType()
                })
            elif layer.type() == _RasterLayerType:
                layer_info.update({
                    "width": layer.width(),
                    "height": layer.height()
                })

            layers.append(layer_info)

        return layers

    def remove_layer(self, layer_id, **kwargs):
        """Remove a layer from the project"""
        project = QgsProject.instance()

        if layer_id in project.mapLayers():
            project.removeMapLayer(layer_id)
            return {"removed": layer_id}
        else:
            raise Exception(f"Layer not found: {layer_id}")

    def zoom_to_layer(self, layer_id, **kwargs):
        """Zoom to a layer's extent"""
        project = QgsProject.instance()

        if layer_id in project.mapLayers():
            layer = project.mapLayer(layer_id)
            self.iface.setActiveLayer(layer)
            self.iface.zoomToActiveLayer()
            return {"zoomed_to": layer_id}
        else:
            raise Exception(f"Layer not found: {layer_id}")

    def get_layer_features(self, layer_id, limit=10, include_geometry=False, **kwargs):
        """Get features from a vector layer with optimized data size

        Args:
            layer_id: The ID of the layer to get features from
            limit: Maximum number of features to return (default: 10)
            include_geometry: Whether to include geometry data (default: False)
        """
        layer = self._get_vector_layer(layer_id)

        features = []

        # Get field names first for the response
        field_names = [field.name() for field in layer.fields()]

        # Always get feature count for metadata
        feature_count = layer.featureCount()

        # Get the actual features
        for i, feature in enumerate(layer.getFeatures()):
            if i >= limit:
                break

            # Extract attributes using the Qt5/Qt6-safe converter
            attrs = {}
            for field in layer.fields():
                attrs[field.name()] = self._convert_to_python_type(
                    feature.attribute(field.name())
                )

            # Create feature object with just the attributes by default
            feature_obj = {
                "id": feature.id(),
                "attributes": attrs,
            }

            # Only include geometry if explicitly requested
            if include_geometry and feature.hasGeometry():
                geom = feature.geometry()
                feature_obj["geometry"] = {
                    "type": geom.type(),
                    "wkt": geom.asWkt(precision=4)
                }

            features.append(feature_obj)

        return {
            "layer_id": layer_id,
            "layer_name": layer.name(),
            "feature_count": feature_count,
            "fields": field_names,
            "features": features,
            "geometry_included": include_geometry,
        }

    def execute_processing(self, algorithm, parameters, **kwargs):
        """Execute a processing algorithm"""
        try:
            import processing
            result = processing.run(algorithm, parameters)
            return {
                "algorithm": algorithm,
                # Convert values to strings for JSON
                "result": {k: str(v) for k, v in result.items()}
            }
        except Exception as e:
            raise Exception(f"Processing error: {str(e)}")

    def save_project(self, path=None, **kwargs):
        """Save the current project"""
        project = QgsProject.instance()

        if not path and not project.fileName():
            raise Exception(
                "No project path specified and no current project path")

        save_path = path if path else project.fileName()
        if project.write(save_path):
            return {"saved": save_path}
        else:
            raise Exception(f"Failed to save project to {save_path}")

    def load_project(self, path, **kwargs):
        """Load a project"""
        project = QgsProject.instance()

        if project.read(path):
            # Bridge stability: do NOT force a canvas refresh through the bridge --
            # on ECW raster + live MSSQL/ODBC projects a bridge-triggered repaint
            # hard-crashes QGIS (NCS::CView / ODBC driver teardown). QGIS repaints
            # the canvas on its own event loop after the project loads.
            return {
                "loaded": path,
                "layer_count": len(project.mapLayers())
            }
        else:
            raise Exception(f"Failed to load project from {path}")

    def create_new_project(self, path, **kwargs):
        """
        Creates a new QGIS project and saves it at the specified path.
        If a project is already loaded, it clears it before creating the new one.

        :param project_path: Full path where the project will be saved
                            (e.g., 'C:/path/to/project.qgz')
        """
        project = QgsProject.instance()

        if project.fileName():
            project.clear()

        project.setFileName(path)
        # Bridge stability: no bridge-triggered canvas refresh here (see load_project) --
        # a fresh/empty project has nothing to repaint and the forced refresh is a
        # documented crash trigger on heavy ECW+ODBC projects.

        # Save the project
        if project.write():
            return {
                "created": f"Project created and saved successfully at: {path}",
                "layer_count": len(project.mapLayers())
            }
        else:
            raise Exception(f"Failed to save project to {path}")

    def set_layer_visibility(self, layer_id, visible, **kwargs):
        """Toggle layer visibility on/off"""
        layer = self._get_layer(layer_id)
        node = QgsProject.instance().layerTreeRoot().findLayer(layer_id)
        if node is None:
            raise Exception(f"Layer tree node not found: {layer_id}")
        node.setItemVisibilityChecked(visible)
        # Bridge stability: repaint ONLY the toggled layer, never force a full
        # mapCanvas().refresh() through the bridge -- that hard-crashes QGIS on
        # ECW raster + live MSSQL/ODBC projects (NCS::CView / ODBC driver teardown).
        layer.triggerRepaint()
        return {"layer_id": layer_id, "layer_name": layer.name(), "visible": visible}

    def filter_layer(self, layer_id, expression="", **kwargs):
        """Set a subset filter (SQL WHERE clause) on a vector layer"""
        layer = self._get_vector_layer(layer_id)
        previous_expression = layer.subsetString()
        if not layer.setSubsetString(expression):
            provider_error = ""
            if layer.dataProvider():
                provider_error = layer.dataProvider().error().message()
            detail = f": {provider_error}" if provider_error else ""
            raise Exception(f"Invalid filter expression: {expression}{detail}")
        return {
            "layer_id": layer_id,
            "layer_name": layer.name(),
            "expression": expression,
            "previous_expression": previous_expression,
            "feature_count": layer.featureCount(),
        }

    def get_layer_fields(self, layer_id, **kwargs):
        """Get field metadata for a vector layer"""
        layer = self._get_vector_layer(layer_id)
        fields = []
        for field in layer.fields():
            fields.append({
                "name": field.name(),
                "type": field.typeName(),
                "length": field.length(),
                "precision": field.precision(),
                "comment": field.comment(),
            })
        return {
            "layer_id": layer_id,
            "layer_name": layer.name(),
            "feature_count": layer.featureCount(),
            "crs": layer.crs().authid(),
            "fields": fields,
        }

    def group_layers(self, name, layer_ids, **kwargs):
        """Create a layer group and move layers into it"""
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.addGroup(name)
        moved = []
        skipped = []
        for lid in layer_ids:
            layer = project.mapLayer(lid)
            if not layer:
                skipped.append(lid)
                continue
            group.addLayer(layer)
            # Remove old node from its parent
            old_node = root.findLayer(lid)
            if old_node is not None and old_node.parent() is not None:
                old_node.parent().removeChildNode(old_node)
            moved.append(lid)
        return {"group": name, "moved_layers": moved, "skipped_layers": skipped}

    def select_features(self, layer_id, expression="", mode="set", **kwargs):
        """Select features by expression, or clear selection if empty.

        Args:
            mode: One of 'set' (default), 'add', 'remove', 'intersect'.
        """
        layer = self._get_vector_layer(layer_id)
        if expression:
            # Map mode string to QgsVectorLayer.SelectBehavior enum (Qt5/Qt6 compat)
            try:
                behavior_map = {
                    "set": QgsVectorLayer.SelectBehavior.SetSelection,
                    "add": QgsVectorLayer.SelectBehavior.AddToSelection,
                    "remove": QgsVectorLayer.SelectBehavior.RemoveFromSelection,
                    "intersect": QgsVectorLayer.SelectBehavior.IntersectSelection,
                }
            except AttributeError:
                behavior_map = {
                    "set": QgsVectorLayer.SetSelection,
                    "add": QgsVectorLayer.AddToSelection,
                    "remove": QgsVectorLayer.RemoveFromSelection,
                    "intersect": QgsVectorLayer.IntersectSelection,
                }
            behavior = behavior_map.get(mode)
            if behavior is None:
                raise Exception(f"Invalid selection mode: {mode}. Use one of: set, add, remove, intersect")
            layer.selectByExpression(expression, behavior)
        else:
            layer.removeSelection()
        selected_ids = layer.selectedFeatureIds()
        return {
            "layer_id": layer_id,
            "layer_name": layer.name(),
            "expression": expression,
            "mode": mode,
            "selected_count": layer.selectedFeatureCount(),
            "selected_ids": list(selected_ids[:100]),
        }

    # --- Async job API (submit_code / poll_job) ---

    def submit_code(self, code, **kwargs):
        """Submit PyQGIS code as an async job; returns a job id immediately.

        The code runs on the Qt main thread on the next event-loop pass, so
        this reply is sent before execution starts and the client's socket
        timeout never races a long-running script. Poll with poll_job.
        """
        self._job_counter += 1
        job_id = f"job_{self._job_counter}"
        self.jobs[job_id] = {"status": "pending", "result": None,
                             "submitted": time.time()}
        QTimer.singleShot(0, lambda: self._run_job(job_id, code))
        # Prune oldest finished jobs so the store can't grow unbounded.
        finished = [jid for jid, j in self.jobs.items()
                    if j["status"] in ("done", "error")]
        for jid in finished[:max(0, len(self.jobs) - 50)]:
            del self.jobs[jid]
        return {"job_id": job_id, "status": "pending"}

    def _run_job(self, job_id, code):
        job = self.jobs.get(job_id)
        if job is None:
            return
        job["status"] = "running"
        result = self.execute_code(code)
        job["result"] = result
        job["status"] = "done" if result.get("executed") else "error"

    def poll_job(self, job_id, **kwargs):
        """Get the status (and, when finished, the result) of a submitted job."""
        job = self.jobs.get(job_id)
        if job is None:
            raise Exception(f"Unknown job id: {job_id}")
        response = {"job_id": job_id, "status": job["status"]}
        if job["status"] in ("done", "error"):
            response["result"] = job["result"]
        return response

    # --- Layer tree API ---

    def _serialize_tree_node(self, node):
        info = {
            "name": node.name(),
            "checked": node.itemVisibilityChecked(),
            "visible": node.isVisible(),
            "expanded": node.isExpanded(),
        }
        if isinstance(node, QgsLayerTreeGroup):
            info["type"] = "group"
            info["children"] = [self._serialize_tree_node(c) for c in node.children()]
        else:
            info["type"] = "layer"
            info["layer_id"] = node.layerId()
        return info

    def get_layer_tree(self, **kwargs):
        """Get the nested layer tree (groups + layers, visibility/expanded state)."""
        root = QgsProject.instance().layerTreeRoot()
        return {"tree": [self._serialize_tree_node(c) for c in root.children()]}

    def _find_tree_node(self, layer_id=None, group_path=None):
        """Resolve a tree node from either a layer id or a group path."""
        root = QgsProject.instance().layerTreeRoot()
        if layer_id:
            node = root.findLayer(layer_id)
            if node is None:
                raise Exception(f"Layer tree node not found: {layer_id}")
            return node
        if group_path:
            node = _find_group_by_path(group_path)
            if node is None:
                raise Exception(f"Group not found: {group_path}")
            return node
        raise Exception("Provide either layer_id or group_path")

    def set_node_visibility(self, visible, layer_id=None, group_path=None,
                            check_ancestors=True, **kwargs):
        """Check/uncheck a layer or group node in the layer tree.

        When enabling with check_ancestors=True (default), ancestor groups are
        checked too so the node actually becomes visible on the canvas.
        No bridge-triggered canvas refresh (crash-safe on ECW+ODBC projects).
        """
        node = self._find_tree_node(layer_id, group_path)
        if visible and check_ancestors:
            _check_with_ancestors(node)
        else:
            node.setItemVisibilityChecked(visible)
        return {
            "node": group_path or layer_id,
            "checked": node.itemVisibilityChecked(),
            "visible": node.isVisible(),
        }

    def move_node(self, layer_id=None, group_path=None, target_group_path=None,
                  index=-1, **kwargs):
        """Move a layer or group into another group (or the root).

        Args:
            layer_id / group_path: the node to move (one of).
            target_group_path: destination group path; None/empty = root.
            index: insert position in the destination (-1 = append). The index
                   applies before the original node is removed.
        """
        node = self._find_tree_node(layer_id, group_path)
        root = QgsProject.instance().layerTreeRoot()
        if target_group_path:
            target = _find_group_by_path(target_group_path)
            if target is None:
                raise Exception(f"Target group not found: {target_group_path}")
        else:
            target = root
        # A group must not be moved into itself or one of its descendants.
        probe = target
        while probe is not None:
            if probe is node:
                raise Exception(
                    "Cannot move a group into itself or its own descendant")
            probe = probe.parent()
        clone = node.clone()
        if 0 <= index <= len(target.children()):
            target.insertChildNode(index, clone)
        else:
            target.addChildNode(clone)
        node.parent().removeChildNode(node)
        return {
            "moved": clone.name(),
            "to": target_group_path or "<root>",
            "index": index,
        }

    def add_group(self, name, parent_path=None, index=-1, **kwargs):
        """Create a layer-tree group under parent_path (root when omitted)."""
        if parent_path:
            parent = _find_group_by_path(parent_path)
            if parent is None:
                raise Exception(f"Parent group not found: {parent_path}")
        else:
            parent = QgsProject.instance().layerTreeRoot()
        if 0 <= index <= len(parent.children()):
            group = parent.insertGroup(index, name)
        else:
            group = parent.addGroup(name)
        path = f"{parent_path}/{name}" if parent_path else name
        return {"group": group.name(), "path": path}

    # --- Canvas extent ---

    def get_extent(self, **kwargs):
        """Get the current canvas extent (read-only; safe on any project)."""
        canvas = self.iface.mapCanvas()
        e = canvas.extent()
        return {
            "xmin": e.xMinimum(), "ymin": e.yMinimum(),
            "xmax": e.xMaximum(), "ymax": e.yMaximum(),
            "crs": QgsProject.instance().crs().authid(),
            "scale": canvas.scale(),
            "width_px": canvas.width(), "height_px": canvas.height(),
        }

    def set_extent(self, xmin, ymin, xmax, ymax, refresh=True, **kwargs):
        """Set the canvas extent (zoom) to the given bbox in project CRS.

        Bridge stability: pass refresh=False on heavy ECW+ODBC projects — a
        bridge-triggered canvas refresh is a documented crash trigger there;
        without it QGIS repaints on its own next canvas interaction.
        """
        canvas = self.iface.mapCanvas()
        canvas.setExtent(QgsRectangle(xmin, ymin, xmax, ymax))
        if refresh:
            canvas.refresh()
        e = canvas.extent()  # canvas adjusts the bbox to its aspect ratio
        return {
            "xmin": e.xMinimum(), "ymin": e.yMinimum(),
            "xmax": e.xMaximum(), "ymax": e.yMaximum(),
            "scale": canvas.scale(),
            "refreshed": bool(refresh),
        }

    def render_map(self, path, width=800, height=600, layer_ids=None,
                   extent=None, **kwargs):
        """Render the map to an image off-screen, without touching the canvas.

        Bridge stability (learned the hard way): the previous implementation used
        the MULTI-threaded QgsMapRendererParallelJob and rendered every checked
        layer. On heavy projects (ECW raster imagery + live MSSQL/ODBC layers,
        e.g. the CBB "Aquila" workspace) the ECW driver mutex (NCS::CView) is
        contended across worker threads and QGIS hard-crashes with an access
        violation during driver teardown -- uncatchable from Python.

        This version renders SINGLE-THREADED via QgsMapRendererCustomPainterJob
        painting onto an off-screen QImage, and never calls mapCanvas().refresh().

        Args:
            path:      output image path.
            width/height: output size in pixels.
            layer_ids: optional list of layer IDs to render. When provided, ONLY
                       those layers are drawn -- pass a vector-only subset to
                       exclude ECW rasters / live ODBC layers from the render.
                       When omitted, falls back to the currently checked layers
                       (WARNING: that fallback can still be heavy and may include
                       ECW/ODBC layers -- prefer passing layer_ids on such projects).
            extent:    optional [xmin, ymin, xmax, ymax] in the project CRS. When
                       omitted, the current canvas extent is read (read-only; safe).
        """
        try:
            project = QgsProject.instance()

            # Resolve the layers to render.
            if layer_ids:
                layers = []
                missing = []
                for lid in layer_ids:
                    lyr = project.mapLayer(lid)
                    if lyr is None:
                        missing.append(lid)
                    else:
                        layers.append(lyr)
                if missing:
                    raise Exception(f"Layer(s) not found: {missing}")
            else:
                # Fallback: currently checked (visible) layers. Can be heavy.
                layers = project.layerTreeRoot().checkedLayers()

            # Create map settings
            ms = QgsMapSettings()
            ms.setLayers(layers)

            # Extent: use provided bbox, else READ (not refresh) the canvas extent.
            if extent:
                xmin, ymin, xmax, ymax = extent
                rect = QgsRectangle(xmin, ymin, xmax, ymax)
            else:
                rect = self.iface.mapCanvas().extent()
            ms.setExtent(rect)

            ms.setOutputSize(QSize(width, height))
            ms.setBackgroundColor(QColor(255, 255, 255))
            ms.setOutputDpi(96)
            # Match the destination CRS to the project so coordinates line up.
            ms.setDestinationCrs(project.crs())

            # Off-screen image to paint onto.
            img = QImage(QSize(width, height), QImage.Format_ARGB32_Premultiplied)
            img.fill(QColor(255, 255, 255))

            painter = QPainter(img)
            t0 = time.perf_counter()
            try:
                # Single-threaded custom-painter job -- no worker threads, so the
                # ECW driver mutex is not contended across threads.
                render = QgsMapRendererCustomPainterJob(ms, painter)
                render.start()
                render.waitForFinished()
            finally:
                painter.end()
            render_seconds = time.perf_counter() - t0

            if img.save(path):
                return {
                    "rendered": True,
                    "path": path,
                    "width": width,
                    "height": height,
                    "layer_count": len(layers),
                    "render_seconds": round(render_seconds, 3)
                }
            else:
                raise Exception(f"Failed to save rendered image to {path}")

        except Exception as e:
            raise Exception(f"Render error: {str(e)}")


class GroupLocatorFilter(QgsLocatorFilter):
    """Locator filter searching layer-tree GROUP names (prefix 'grp').

    QGIS's built-in 'l' filter only searches layer names; this one searches
    group paths and toggles the activated group's visibility, checking
    ancestor groups when enabling so the group actually shows.

    Threading contract (gotchas from live prototyping):
    - prepare() runs on the MAIN thread and must return a list of strings
      (QStringList) — returning None raises "TypeError: invalid result from
      prepare()". The group snapshot is built here.
    - fetchResults() runs on a WORKER thread and must only touch the snapshot.
    - triggerResult() runs on the main thread again.
    - Non-core filters need a prefix of >= 3 chars ('grp', not 'g') — shorter
      prefixes are reserved for core filters and silently dropped. Users can
      rebind a shorter prefix in Settings > Options > Locator.
    """

    def __init__(self, iface):
        super().__init__()
        self.iface = iface
        self._groups = []

    def clone(self):
        return GroupLocatorFilter(self.iface)

    def name(self):
        return "qgis_mcp_groups"

    def displayName(self):
        return "Layer Tree Groups"

    def prefix(self):
        return "grp"

    def prepare(self, string, context):
        # Main thread: snapshot (path, checked) for the worker thread.
        self._groups = [(path, node.itemVisibilityChecked())
                        for path, node in _iter_group_paths()]
        return []  # autocomplete suggestions; must be a QStringList, not None

    def fetchResults(self, string, context, feedback):
        needle = (string or "").lower()
        for path, checked in self._groups:
            if feedback.isCanceled():
                return
            if needle and needle not in path.lower():
                continue
            result = QgsLocatorResult()
            result.filter = self
            result.displayString = path
            result.description = ("visible — activate to hide" if checked
                                  else "hidden — activate to show")
            result.score = 1.0 if path.lower().startswith(needle) else 0.5
            try:
                result.setUserData(path)  # QGIS 3.34+
            except AttributeError:
                result.userData = path
            self.resultFetched.emit(result)

    def triggerResult(self, result):
        data = getattr(result, "userData", None)
        if callable(data):  # newer API exposes userData() as a getter
            data = data()
        node = _find_group_by_path(data) if data else None
        if node is None:
            QgsMessageLog.logMessage(
                f"Group no longer exists: {data}", "QGIS MCP", Qgis.Warning)
            return
        if node.itemVisibilityChecked():
            node.setItemVisibilityChecked(False)
        else:
            _check_with_ancestors(node)


class QgisMCPDockWidget(QDockWidget):
    """Dock widget for the QGIS MCP plugin"""
    closed = pyqtSignal()

    def __init__(self, iface):
        super().__init__("QGIS MCP")
        self.iface = iface
        self.server = None
        self.setup_ui()

    def setup_ui(self):
        """Set up the dock widget UI"""
        # Create widget and layout
        widget = QWidget()
        layout = QVBoxLayout()
        widget.setLayout(layout)

        # Add port selection
        layout.addWidget(QLabel("Server Port:"))
        self.port_spin = QSpinBox()
        self.port_spin.setMinimum(1024)
        self.port_spin.setMaximum(65535)
        self.port_spin.setValue(9876)
        layout.addWidget(self.port_spin)

        # Add server control buttons
        self.start_button = QPushButton("Start Server")
        self.start_button.clicked.connect(self.start_server)
        layout.addWidget(self.start_button)

        self.stop_button = QPushButton("Stop Server")
        self.stop_button.clicked.connect(self.stop_server)
        self.stop_button.setEnabled(False)
        layout.addWidget(self.stop_button)

        # Add status label
        self.status_label = QLabel("Server: Stopped")
        layout.addWidget(self.status_label)

        # Add to dock widget
        self.setWidget(widget)

    def start_server(self):
        """Start the server"""
        if not self.server:
            port = self.port_spin.value()
            self.server = QgisMCPServer(port=port, iface=self.iface)

        if self.server.start():
            self.status_label.setText(
                f"Server: Running on port {self.server.port}")
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.port_spin.setEnabled(False)

    def stop_server(self):
        """Stop the server"""
        if self.server:
            self.server.stop()
            self.server = None

        self.status_label.setText("Server: Stopped")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.port_spin.setEnabled(True)

    def closeEvent(self, event):
        """Stop server on dock close"""
        self.stop_server()
        self.closed.emit()
        super().closeEvent(event)


class QgisMCPPlugin:
    """Main plugin class for QGIS MCP"""

    def __init__(self, iface):
        self.iface = iface
        self.dock_widget = None
        self.action = None
        self.locator_filter = None

    def initGui(self):
        """Initialize GUI"""
        # Create action
        self.action = QAction(
            "QGIS MCP",
            self.iface.mainWindow()
        )
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_dock)

        # Add to plugins menu and toolbar
        self.iface.addPluginToMenu("QGIS MCP", self.action)
        self.iface.addToolBarIcon(self.action)

        # Group locator filter ('grp <text>' in the locator bar) — registered
        # here, independent of the MCP server, so it survives QGIS restarts.
        self.locator_filter = GroupLocatorFilter(self.iface)
        self.iface.registerLocatorFilter(self.locator_filter)

    def toggle_dock(self, checked):
        """Toggle the dock widget"""
        if checked:
            # Create dock widget if it doesn't exist
            if not self.dock_widget:
                self.dock_widget = QgisMCPDockWidget(self.iface)
                self.iface.addDockWidget(
                    _RightDockWidgetArea, self.dock_widget)
                # Connect close event
                self.dock_widget.closed.connect(self.dock_closed)
            else:
                # Show existing dock widget
                self.dock_widget.show()
        else:
            # Hide dock widget
            if self.dock_widget:
                self.dock_widget.hide()

    def dock_closed(self):
        """Handle dock widget closed"""
        self.action.setChecked(False)

    def unload(self):
        """Unload plugin"""
        # Deregister the locator filter (this also deletes it)
        if self.locator_filter:
            self.iface.deregisterLocatorFilter(self.locator_filter)
            self.locator_filter = None

        # Stop server if running
        if self.dock_widget:
            self.dock_widget.stop_server()
            self.iface.removeDockWidget(self.dock_widget)
            self.dock_widget = None

        # Remove plugin menu item and toolbar icon
        self.iface.removePluginMenu("QGIS MCP", self.action)
        self.iface.removeToolBarIcon(self.action)


# Plugin entry point
def classFactory(iface):
    return QgisMCPPlugin(iface)
