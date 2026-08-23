# TODO

Deficiencies observed while driving QGIS through the MCP from Claude Code
(2026-08-22, building a 419-layer group/map tree from a Minex GM3/GPKG —
see `~/Github/Minex` and the `minex-style` skill there).

All items below were implemented 2026-08-23 and verified with the live test
suite (`tests/test_qgis_live.py`, 17 passed) against a running QGIS.

## Protocol / transport

- [x] **Request/response correlation (worst issue).** Every request now
  carries a monotonically increasing `id` which the plugin echoes in the
  response. The client discards stale frames whose id doesn't match, and on
  a timeout closes the socket (the stream may be mid-frame) so the next call
  reconnects fresh — later calls can no longer receive an earlier call's
  result. Regression test: `test_stale_reply_does_not_desync`.
- [x] **Long-running calls.** `send_command` takes a per-call `timeout`
  (default 30 s, overridable via `QGIS_MCP_TIMEOUT`), exposed as a `timeout`
  parameter on `execute_code`, `execute_processing`, `render_map`, and
  `load_project`. Added an async job API: `submit_code` returns a job id
  immediately (the code runs on the next Qt event-loop pass), `poll_job`
  fetches status/result. Completed jobs are retained (last ≤50) so a client
  that timed out polling can still fetch the result.
- [x] **Reconnect after QGIS restart.** On a dead/stale socket (send or
  receive `OSError`, e.g. `WinError 10053`) the client disconnects,
  reconnects, and retries the command once — transparently within the same
  call. Test: `test_reconnect_after_disconnect`.

## Missing tools

- [x] **Layer-tree API.** Added `get_layer_tree` (nested groups + layers
  with checked/visible/expanded state), `set_node_visibility` (by layer id
  or `/`-separated group path, with `check_ancestors`), `move_node`, and
  `add_group`.
- [x] **Canvas extent get/set.** Added `get_extent` (bbox, CRS, scale,
  canvas size) and `set_extent` (with a `refresh=False` precaution option;
  the historical ECW+ODBC refresh crash did not reproduce in live
  verification on such a project, 2026-08-23 — see the render note below).
- [x] **`render_map` timing.** The result now includes `render_seconds`
  (render duration, excluding image save) and `layer_count`.

  *ECW landmine cleared (2026-08-23)*: the historical hard-crash rendering
  ECW+ODBC projects was specific to the old multi-threaded renderer. The
  single-threaded off-screen render was verified live on the CBB workspace
  (Capcoal 20 cm ECW + 13 mssql layers): ECW alone, ECW+mssql, the
  all-checked-layers fallback, and a bridge-triggered full canvas refresh
  all survived. Rendering ECW/ODBC layers through the bridge is safe.

## Feature ideas

- [x] **Group locator filter.** `GroupLocatorFilter` (prefix `grp`) is now
  registered by the plugin in `initGui` — independent of the MCP server, so
  it survives restarts. Searches `/`-separated group paths and toggles the
  activated group's visibility, checking ancestors when enabling. The
  prototyping gotchas are baked in: `prepare()` builds the group snapshot on
  the main thread and returns a QStringList (never `None`); `fetchResults()`
  only touches the snapshot on the worker thread; the prefix is 3 chars
  (`grp`) because shorter prefixes are silently reserved for core filters
  (rebindable in Settings > Options > Locator).
