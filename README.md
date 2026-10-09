# FMU Executor

Shared Python/FastAPI service for the Windows and Linux Lab Station
implementations. `VERSION` is the source release version; the Lab Station
repositories carry release snapshots so station releases do not download
executable code at runtime. Lab Gateway's `fmu-runner` consumes the service in
`station` backend mode.

Install from this project with `python -m pip install .` or use
`requirements.txt` to build a station-local virtual environment. Keep the
same release version in both Lab Station snapshots.

Python 3.11 or newer is required by the supported runtime dependencies.

## Quick start

```bash
cd fmu-executor
pip install -r requirements.txt
python -m app
```

The service listens on `http://0.0.0.0:8091` by default. It is an internal
station service; expose the configured port only to the Lab Gateway network and
configure the same non-empty `FMU_INTERNAL_TOKEN` in the Station process
environment and Gateway's `FMU_STATION_INTERNAL_TOKEN`.

The repository's `Dockerfile` builds the container used by Lab Gateway's
optional local Executor profile. Pushing a `vX.Y.Z` tag publishes
`ghcr.io/decentralabscom/fmu-executor:X.Y.Z`; the tag must match both
`VERSION` and the package version in `pyproject.toml`. Gateway pins a
versioned image by default, so installing that profile does not require a
separate FMU-Executor checkout. Station releases continue to package the
service through their platform installers.

After the first image is published, set its GitHub Container Registry package
visibility to Public if Gateway installations must pull it without GitHub
authentication. The source repository is linked automatically by the image's
OCI source label and the publishing workflow.

On Windows, when Lab Station starts the sidecar through `LabStation\BackgroundService`,
Windows Task Scheduler runs that task as `SYSTEM`. A per-user Python install or
user-scoped `pip install` is not visible to that account. Install the
requirements into a machine-wide Python environment (or a virtual environment
under the Lab Station installation directory) and make sure the interpreter and
packages are readable by `SYSTEM`. Verify the installation in a SYSTEM shell
before starting the task, for example:

```powershell
psexec -accepteula -s -i powershell.exe
python -m pip install -r C:\LabStation\fmu-executor\requirements.txt
python -c "import uvicorn"
```

The Python interpreter used by the task must resolve the same environment; do
not rely on packages installed only for the interactive administrator account.

The same integration is visible from the Lab Station **Connectors** panel,
which reports the FMU endpoint, local model directory, port, token status, and
the Gateway environment expected for station mode:

![Lab Station FMI/FMU connector](../docs/images/labstation-connectors-fmi.png)

*FMI/FMU connector view from the Lab Station desktop UI.*

## Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `FMU_EXECUTOR_HOST` | `0.0.0.0` | Bind address |
| `FMU_EXECUTOR_PORT` | `8091` | Bind port |
| `FMU_ROOT` | `./fmu-data` | Directory with provisioned `.fmu` files |
| `FMU_INTERNAL_TOKEN` | *(required)* | Shared secret for `X-Internal-Session-Token`; requests fail closed when it is absent |
| `FMU_INTERNAL_TOKEN_FILE` | *(unset)* | Optional path to a mounted token file; used when the direct token and base64 token are unset |
| `FMU_MAX_SESSIONS` | `4` | Effective max concurrent FMU executions (one-shot, stream and realtime) |
| `FMU_ATTACH_GRACE_SECONDS` | `120` | How long a disconnected realtime session remains attachable before its FMU state is terminated |
| `FMU_EXECUTOR_TEMP` | `<FMU_ROOT>/.tmp` | Temp dir for FMU extraction |
| `FMU_EXECUTION_MODE` | `process` | One-shot/stream execution boundary: `process` (recommended) or `in-process` (diagnostics) |
| `FMU_EXECUTION_TIMEOUT_SECONDS` | `3600` | Deadline for an isolated one-shot/stream worker |
| `FMU_OMSIMULATOR_ENABLED` | `false` | Enables OMSimulator discovery for the future composition backend; does not switch current execution automatically |
| `FMU_OMSIMULATOR_COMMAND` | `OMSimulator` | OMSimulator executable or command name |
| `FMU_OMSIMULATOR_TIMEOUT_SECONDS` | `3600` | Reserved timeout for the future OMSimulator adapter |
| `FMU_LOG_LEVEL` | `INFO` | Log level |

For the Windows scheduled task, set the token as a machine-level environment
variable before starting the Station service:

```powershell
[Environment]::SetEnvironmentVariable('FMU_INTERNAL_TOKEN', '<random-shared-secret>', 'Machine')
```

Restart the `LabStation\BackgroundService` task after changing the
variable. The token value must be copied to Lab Gateway's
`FMU_STATION_INTERNAL_TOKEN`; do not put it in source control or in a
public connector URL. `FMU_EXECUTOR_PORT` must be a numeric TCP port from 1 to
65535. The Lab Station supervisor reads the same variable for its health checks
and creates the `LabStation-FMU-Executor` inbound rule for that port on Domain
and Private profiles. A manual `python -m app` launch does not create the
firewall rule.

## Internal API

All endpoints require `X-Internal-Session-Token` header (except `/internal/health`).

| Method | Path | Description |
|---|---|---|
| GET | `/internal/health` | Health & diagnostics |
| GET | `/internal/fmu/catalog` | FMU inventory; `X-FMU-Access-Key` header |
| GET | `/internal/fmu/describe` | Model description; `X-FMU-Access-Key` header |
| GET | `/internal/fmu/capacity` | Effective execution capacity; internal token required |
| POST | `/internal/fmu/validate/{access_key}?auto_quarantine=false` | Validates an FMU and optionally quarantines it when invalid |
| POST | `/internal/fmu/quarantine/{access_key}?reason=manual` | Explicitly quarantines an FMU |
| DELETE | `/internal/fmu/quarantine/{access_key}` | Removes an FMU from quarantine |
| GET | `/internal/fmu/quarantine` | Lists quarantined FMUs |
| POST | `/internal/fmu/simulations/run` | One-shot simulation run; JSON body contains `accessKey` |
| POST | `/internal/fmu/simulations/stream` | Streaming NDJSON simulation; JSON body contains `accessKey` |
| WS | `/internal/fmu/sessions` | Realtime session (step, setInputs, getOutputs, authenticated reconnect) |

`catalog` and `describe` also require the `X-FMU-Access-Key` header. The
`access_key` path segment is URL-encoded when it contains nested directories.

`GET /internal/fmu/backends` reports the active FMPy backend and the planned
OMSimulator backend. The same information is included in `/internal/health`.

## Runtime capabilities

The executor uses FMPy `0.3.32` through `instantiate_fmu`, so the Station
runtime selects the FMI 2 or FMI 3 Co-Simulation implementation from the FMU
model description. Realtime input/output handling includes scalar and array
variables for the FMI numeric types, Boolean, String, Binary and Clock where
the FMU runtime exposes the corresponding API. Binary values use base64 in the
JSON contract, and FMI 3 `Int64`/`UInt64` outputs are serialized as strings to
avoid loss of precision in JavaScript clients.

One-shot and NDJSON streaming simulations run in spawned worker processes.
Each realtime session also owns a separate spawned worker; the API process keeps
only the session registry and sends an allowlisted control set over private IPC.
This keeps native FMU crashes and blocking calls scoped to one session while
preserving session reconnects during the configured grace period. Set
`FMU_EXECUTION_MODE=in-process` only for diagnostics or environments where
native FMU isolation is managed elsewhere.

The realtime session advertises `start`, `pause`, `resume`, `reset`, `step`,
input/output and reconnect capabilities. `reset` recreates the FMU instance
with the original initialization options and inputs.

### FMI conformance baseline

| Capability | Station status | Scope |
|---|---|---|
| FMI 2 Co-Simulation | Supported | Realtime, one-shot and stream |
| FMI 3 Co-Simulation | Supported | Realtime, one-shot and stream |
| Scalar Real/Float32/Float64, integer, Boolean, String | Supported | Inputs and outputs |
| FMI 3 arrays | Supported | Fixed-size arrays resolved by FMPy |
| FMI 3 Binary and Clock | Supported | JSON uses base64 for Binary |
| FMI 2/FMI 3 Model Exchange | Planned | Requires a solver/composition boundary; not claimed by the Station realtime API |
| FMI 3 Scheduled Execution | Planned | Not exposed by the current Station contract |
| SSP/multi-FMU composition | Planned | OMSimulator adapter |

This table is the contract baseline for adding real FMU fixtures to the
conformance suite; a new type must not be advertised as supported only because
its model description can be parsed.

### OMSimulator (future composition backend)

OMSimulator is not a mandatory dependency of the Windows Station runtime and
is not used for ordinary single-FMU requests today. The executor exposes its
planned status and reserves `options.backend: "omsimulator"` for a future
adapter that will execute SSP/multi-FMU compositions and Model Exchange
scenarios. Until that adapter is implemented, such a request returns HTTP
`501`; this keeps the backend choice explicit rather than silently pretending
that a single-FMU FMPy execution was a composed model.

### HTTP simulation payloads

The one-shot and streaming endpoints accept this body shape:

```json
{
  "accessKey": "Heater.fmu",
  "parameters": {"ambient": 293.15},
  "options": {"startTime": 0, "stopTime": 10, "stepSize": 0.01},
  "claims": {},
  "labId": "lab-01",
  "reservationKey": "reservation-123"
}
```

`run` returns one `sim.result` object with `time`, `state: "terminated"`, and
`outputs`. `stream` returns newline-delimited JSON: `sim.step` snapshots with
`seq`, `time`, and `outputs`, followed by `sim.done`; capacity or execution
failures are emitted as an `error` object with a short `code` and, where
applicable, `retryable: true`.

### Realtime WebSocket protocol

Connect to `/internal/fmu/sessions` with the `X-Internal-Session-Token` header.
Every message is a JSON object; an optional `requestId` is echoed in the
response. `session.create` and `session.attach` require a `gatewayContext`
containing the FMU `accessKey` and the reservation bindings (`sub`, `labId`,
`reservationKey`, and active `claims`, including `exp` when supplied by the
Gateway).

```json
{
  "type": "session.create",
  "requestId": "req-1",
  "gatewayContext": {
    "accessKey": "Heater.fmu",
    "sub": "student-42",
    "labId": "lab-01",
    "reservationKey": "reservation-123",
    "claims": {"accessKey": "Heater.fmu", "exp": 1893456000}
  }
}
```

The main request/response types are:

| Request `type` | Response `type` | Required fields / effect |
|---|---|---|
| `session.create` | `session.created` | Creates and loads a session; returns `sessionId`, expiry, and capabilities |
| `session.attach` | `session.attached` | Reconnects a detached session during `FMU_ATTACH_GRACE_SECONDS` after validating the original context |
| `model.describe` | `model.description` | Returns normalized model metadata |
| `sim.initialize` | `sim.state` | Optional `options.startTime`, `stopTime`, `stepSize`, and `parameters` |
| `sim.start` | `sim.state` | Starts automatic stepping until `stopTime` |
| `sim.pause` | `sim.state` | Pauses automatic stepping while retaining FMU state |
| `sim.resume` | `sim.state` | Resumes automatic stepping from `paused` or `initialized` |
| `sim.reset` | `sim.state` | Recreates the FMU instance with the original initialization options |
| `sim.step` | `sim.outputs` | `deltaT` (legacy `stepSize` accepted) |
| `sim.runUntil` | `sim.outputs` | `time` (legacy `targetTime` accepted); optional `stepSize` |
| `sim.setInputs` | `sim.inputs.updated` | `values` map keyed by model variable name |
| `sim.getOutputs` | `sim.outputs` | Optional `variables` array (legacy `valueReferences` accepted) |
| `sim.subscribeOutputs` | `sim.subscribed` | Optional `variables`, `periodMs`, `maxBatchSize`, `maxHz` |
| `sim.unsubscribeOutputs` | `sim.unsubscribed` | Stops background output events |
| `sim.getState` | `sim.state` | Returns current simulation state and time |
| `session.ping` / `ping` | `session.pong` | Liveness check |
| `session.terminate` | `session.closed` | Terminates the FMU session and temporary state |

Subscribed output events use `type: "sim.outputs"` and include `sessionId`,
monotonic `seq`, `simTime`, `values`, `batchSize`, and `dropped`. Errors use
`type: "error"`, a short `code`, a safe `message`, and `retryable`.

When the internal WebSocket closes unexpectedly, the executor detaches the
session instead of immediately terminating it. A new internal connection may
send `session.attach` with the original `sessionId` and validated
`gatewayContext` during `FMU_ATTACH_GRACE_SECONDS`. The original subject, lab,
FMU access key and reservation bindings must match; otherwise the attach is
rejected. Once the grace window expires, the FMU state and temporary files are
cleaned up.

## FMU provisioning

Place `.fmu` files directly in `fmu-data/`:

```
fmu-data/
  Heater.fmu          # accessKey = "Heater.fmu"
  my-model/
    model.fmu          # accessKey = "my-model"
```

## Tests

```bash
cd fmu-executor
pip install pytest httpx
python -m pytest tests/ -v
```
