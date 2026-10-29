# Private API

FMU Executor exposes an internal interface for Lab Gateway and the Lab Station
supervisor. It is not a public reservation API. Gateway owns public access,
reservation authorization, AAS policy application and browser-facing routes;
see the [Lab Gateway FMI/FMU guide](https://github.com/DecentraLabsCom/Lab-Gateway/blob/main/docs/fmi-fmu-support.md).

## Authentication and access keys

HTTP requests must provide `X-Internal-Session-Token`, except
`GET /internal/health`. FMU-specific operations also use
`X-FMU-Access-Key`. The value identifies an FMU beneath `FMU_ROOT`; URL-encode
it when it contains path separators. Gateway-to-Executor simulation requests
carry a validated `gatewayContext` with the reservation scope. The Executor
uses that context for reservation deadlines, quotas and stored-result scoping.

The token authenticates the private service channel. It is not a substitute for
Gateway authorization or a user-facing credential.

## HTTP endpoints

| Method | Path | Purpose and requirements |
|---|---|---|
| `GET` | `/internal/health` | Health and runtime diagnostics; no internal token. |
| `GET` | `/internal/fmu/capacity` | Effective concurrent execution capacity; internal token. |
| `GET` | `/internal/fmu/backends` | Active FMPy backend and optional OMSimulator discovery; internal token. |
| `GET` | `/internal/fmu/catalog` | FMU inventory; internal token and `X-FMU-Access-Key`. |
| `GET` | `/internal/fmu/describe` | Normalized model description; internal token and `X-FMU-Access-Key`. Optional `publicOnly=true` and `causality=output` filters. |
| `GET` | `/internal/fmu/companion-asset` | A declared local visual asset from FMI-LS-REF or a same-stem companion; choose `assetType=auto`, `glb` or `threejs-app`; internal token and FMU access key. Auto prefers a `.webapp.zip`, then a declared GLB, then a same-stem GLB, and reports the resolved type and source in response headers. |
| `GET` | `/internal/fmu/companion-app/{asset_path}` | Reads one safe file from the same-stem `.webapp.zip`; internal token and FMU access key. |
| `POST` | `/internal/fmu/validate/{access_key}?auto_quarantine=false&probe=false` | Validates archive, model description and Station compatibility; optional isolated execution probe. |
| `POST` | `/internal/fmu/quarantine/{access_key}?reason=manual` | Quarantines an FMU. |
| `DELETE` | `/internal/fmu/quarantine/{access_key}` | Removes an FMU from quarantine. |
| `GET` | `/internal/fmu/quarantine` | Lists quarantined FMUs. |
| `POST` | `/internal/fmu/simulations/run` | Synchronous one-shot simulation; body contains `accessKey`. |
| `POST` | `/internal/fmu/simulations/stream` | Streaming NDJSON simulation; body contains `accessKey`. |
| `POST` | `/internal/fmu/simulations/jobs` | Submits a reservation-scoped cancellable simulation job. |
| `POST` | `/internal/fmu/simulations/batches` | Submits a bounded reservation-scoped batch. |
| `GET` | `/internal/fmu/simulations/history` | Pages the current reservation's stored history; requires `X-Gateway-Context`. |
| `GET` | `/internal/fmu/simulations/{job_id}` | Reads reservation-scoped job status. |
| `POST` | `/internal/fmu/simulations/{job_id}/cancel` | Cancels a reservation-scoped job. |
| `GET` | `/internal/fmu/simulations/{job_id}/result` | Reads a reservation-scoped terminal result. |
| `WS` | `/internal/fmu/sessions` | Realtime session protocol for simulation control and output reads. |

All endpoints except health require the internal token. Each model-specific
route identifies its FMU using the header, URL path or request body shown in
the table. Simulation and stored-job routes validate the Gateway reservation
context; only the legacy synchronous `run` and `stream` paths can be called
without it over the internal token-protected channel, and those direct calls do
not create reservation-scoped history or consume reservation scenario quota.

For `assetType=auto`, the Executor reads the FMI-LS-REF manifest from inside
the FMU and accepts only local declared assets beneath `FMU_ROOT`; external
REF URLs are not fetched. It prefers a declared or same-stem
`{model}.webapp.zip`, then a declared GLB, then a same-stem `{model}.glb`.
Companion lookup rejects paths outside `FMU_ROOT` and enforces size limits.
`companion-app` reads a bounded file from the ZIP without extracting the
archive to disk.

## Synchronous simulation requests

Gateway requests use this shape for `run` and `stream`:

```json
{
  "accessKey": "Heater.fmu",
  "simId": "gateway-generated-id",
  "gatewayContext": {
    "accessKey": "Heater.fmu",
    "labId": "lab-01",
    "reservationKey": "reservation-123",
    "claims": {
      "accessKey": "Heater.fmu",
      "labId": "lab-01",
      "reservationKey": "reservation-123",
      "pucHash": "...",
      "exp": 1893456000
    }
  },
  "parameters": {"ambient": 293.15},
  "options": {"startTime": 0, "stopTime": 10, "stepSize": 0.01}
}
```

`run` returns a `sim.result` object containing `time`, `state: "terminated"`
and `outputs`. `stream` returns newline-delimited JSON: `sim.step` snapshots
with `seq`, `time` and `outputs`, followed by `sim.done`. Failures are emitted
as an `error` object with a short `code` and, when applicable,
`retryable: true`. On success, `sim.done.evidence` contains runtime metadata and
the SHA-256 of the preceding NDJSON data lines.

Options can select `outputVariables` and reduce output volume with
`outputSampleEverySteps`. Result and history storage limits remain enforced.
The supported request fields and runtime limits are described in the
[runtime guide](runtime.md) and [configuration reference](configuration.md).

## Jobs and batches

The private job and batch endpoints are used by Gateway, not by Marketplace or
browser clients directly. Requests must include a complete Gateway reservation
scope. Job status, cancellation, result and history reads are filtered to that
same scope; the internal token authenticates the connection, while the stored
scope hash enforces ownership. Asynchronous jobs reject a non-empty
`inputSchedule`; batch cases do not expose an input-schedule field.

Jobs reserve an execution slot before their scenario quota is charged. Capacity
admission is immediate: if no slot is available, the service returns
`STATION_CAPACITY_EXHAUSTED` (HTTP 429). `queued` means an admitted worker is
starting; it does not represent a waiting queue. A worker interrupted by a
service restart is recorded as interrupted and is not resumed. Terminal partial
results remain available until retention or storage pruning removes their
output data.

Exact admission, quota, retention and payload-size defaults are listed in the
[configuration reference](configuration.md). Their enforcement and reservation
semantics are detailed in the [runtime guide](runtime.md).

## Realtime WebSocket protocol

Connect to `/internal/fmu/sessions` and authenticate the WebSocket handshake
with `X-Internal-Session-Token`. Each message is a JSON object. `requestId` is
optional and, when present, is echoed in the response. `session.create` and
`session.attach` require `gatewayContext` with the FMU access key and
reservation bindings (`sub`, `labId`, `reservationKey`, and active `claims`,
including `exp` when provided by Gateway).

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

| Request `type` | Response `type` | Fields and effect |
|---|---|---|
| `session.create` | `session.created` | Loads a session; returns `sessionId`, expiry and capabilities. |
| `session.attach` | `session.attached` | Reattaches a disconnected session during the configured grace window after validating its original context. |
| `model.describe` | `model.description` | Returns normalized FMU metadata. `includeVariables: false` omits the variable list. |
| `sim.initialize` | `sim.state` | Accepts `options.startTime`, `stopTime`, `stepSize` and initial `parameters`. |
| `sim.start` | `sim.state` | Starts automatic stepping through `stopTime`. |
| `sim.pause` | `sim.state` | Pauses automatic stepping while retaining FMU state. |
| `sim.resume` | `sim.state` | Resumes from `paused` or `initialized`. |
| `sim.reset` | `sim.state` | Recreates the FMU instance with the original initialization options. |
| `sim.step` | `sim.outputs` | Advances by `deltaT`; legacy `stepSize` is accepted. The response includes FMI event/early-return/termination flags and a step `sequence` for ordering same-time event samples. |
| `sim.runUntil` | `sim.outputs` | Advances to `time`; legacy `targetTime` is accepted. Optional `stepSize`. |
| `sim.advance` | `sim.samples` | Advances toward `targetTime` and returns bounded intermediate samples; optional `stepSize`, `maxSamples` (1–256), `maxComputeMs` (1–1000, soft), `outputSampleEverySteps`, `outputVariables`, `inputValues` and `inputSchedule`. Supports `external` mode when `currentCommunicationPoint` matches the current simulation time and the target does not exceed `stopTime`. |
| `sim.setInputs` | `sim.inputs.updated` | `values` map keyed by model variable name. |
| `sim.getOutputs` | `sim.outputs` | Optional `variables` array; legacy `valueReferences` accepted. |
| `sim.subscribeOutputs` | `sim.subscribed` | Optional `variables`, `periodMs`, `maxBatchSize` and `maxHz`. |
| `sim.unsubscribeOutputs` | `sim.unsubscribed` | Stops background output events. |
| `sim.getState` | `sim.state` | Returns current session state and simulation time. |
| `session.ping` / `ping` | `session.pong` | Liveness check. |
| `session.terminate` | `session.closed` | Terminates the session and its temporary state. |

Runtime writes accept JSON integer values for integer FMI variables and
canonical decimal strings where a client needs lossless 64-bit transport.
`Binary` values use base64 strings; array variables use arrays of the same
transport representation. The Executor decodes these values only after
validating the request and calculates input quotas from the original JSON
representation.

Subscribed output events use `type: "sim.outputs"` and include `sessionId`,
monotonic `seq`, `simTime`, `values`, `batchSize` and `dropped`. Errors use
`type: "error"`, a short `code`, a safe `message` and `retryable`. Temporary
input write limits return `INPUT_UPDATE_RATE_LIMIT` and `retryAfterMs`; a timed
schedule that exceeds one advance's quota returns `INPUT_UPDATE_BATCH_LIMIT`
with `maxBatchSize`.

The shared `sim.advance` operation was introduced in FMU Executor 0.3.2.
It returns `sim.samples` with intermediate samples, `simTime`, `finished`,
`finishReason`, `stopReason`, `stepsExecuted`, `inputEventsApplied`,
`samplesReturned`, `sampleBytes`, and `computeMs`. A requested `maxComputeMs`
adds `maxComputeMs` and `computeBudgetOverrunMs`. Its frame size remains below a
conservative 1 MB budget; the Executor can reduce the returned batch size when
output names are long. Session capabilities advertise `advanceSamples`,
`advanceComputeBudget`, `advanceSampleSequences`, `maxOutputHz`,
`advanceInputValues` and `inputUpdateQuota`. The latter reports sliding-window
and daily quotas, maximum payload size, and maximum scheduled events and bytes
accepted per `sim.advance`. A `null` sliding-window or daily quota means that
quota is unlimited. `maxUpdatesPerWindow` reflects the effective signed
per-resource policy when one is configured; per-advance event and byte bounds
remain finite.

The `maxComputeMs` budget is soft and is checked between FMI steps. It cannot
interrupt a long-running `doStep()`: the Executor returns after that step and
reports any overrun. `stopReason` is `target_reached`, `max_samples`,
`compute_budget`, `early_return`, or `simulation_finished`. An adaptive client
can compare `computeMs` with the full request cycle and use measured payload
bytes to distinguish model cost from transport cost. The budget never changes
`stepSize`.

For high-rate models, `outputSampleEverySteps` reads and returns every Nth
integration sample while still executing each FMI step. The final sample,
event/early-return samples and a sample at a compute-budget stop are also
included. The request must still fit within `maxSamples` and the frame budget.
`outputVariables` can select named FMU outputs to reduce each sample.
`inputValues` applies a runtime write set at the current simulation time before
the first step. It counts as one input update and does not need to align with
the global step grid, which lets FMI clients couple inputs to their chosen
communication points. `inputSchedule` can carry up to the advertised
`maxScheduledEventsPerAdvance` ordered, step-aligned timed writes for that
advance window. `inputValues` and `inputSchedule` share the event and encoded
byte bounds; the Executor validates every write against the session's variable
permissions and applies it at its scheduled model time while advancing. These
writes share the session's realtime input quotas.
`inputEventsApplied` reports the consumed prefix; only that prefix consumes
quota, so clients can resend the remaining events without charging them twice.
Each sample has a monotonically increasing `sequence` and may share its
simulation time with another sample when an FMI event returns at the current
communication point. Event and early-return flags stay attached to their
sample. These fields are optional; ordinary apps can continue using
`sim.advance` as before.

If the internal WebSocket closes unexpectedly, the Executor detaches the
session instead of terminating it immediately. A new connection may attach
using the original `sessionId` and validated `gatewayContext` within
`FMU_ATTACH_GRACE_SECONDS`. The original subject, lab, FMU access key and
reservation bindings must match. When the grace period expires, FMU state and
temporary files are removed.
