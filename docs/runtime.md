# Runtime behavior and FMI support

This guide describes what FMU Executor loads, validates and runs. Lab Gateway
is the public authorization boundary: it resolves and applies the AAS variable
access policy before forwarding permitted work. Executor validates the FMI
interface and execution request, applies reservation context and resource
limits, and runs the FMU. See the Gateway
[FMI/FMU guide](https://github.com/DecentraLabsCom/Lab-Gateway/blob/main/docs/fmi-fmu-support.md)
for public access and AAS policy behavior.

## FMI execution scope

FMU Executor uses FMPy `0.3.32` through `instantiate_fmu`. It selects the FMI 2
or FMI 3 Co-Simulation implementation described by the FMU.

| Capability | Status | Scope |
|---|---|---|
| FMI 2 Co-Simulation | Supported | Realtime, one-shot and streaming execution. |
| FMI 3 Co-Simulation | Supported | Realtime, one-shot and streaming execution, including event handling supported by the FMU/runtime. |
| Scalar Real/Float32/Float64, integer, Boolean and String | Supported | Inputs and outputs exposed by the runtime. |
| FMI 3 fixed-size arrays | Supported | Resolved by FMPy. |
| FMI 3 Binary and Clock | Supported | Where the FMU runtime exposes the corresponding API; Binary uses base64 in JSON. |
| FMI 3 `Int64` and `UInt64` | Supported | Serialized as strings in JSON to avoid JavaScript precision loss. |
| FMI 2/FMI 3 Model Exchange | Not executable | The Station execution contract is limited to Co-Simulation. |
| FMI 3 Scheduled Execution | Planned | Not exposed by the current Station contract. |
| SSP / multi-FMU composition | Planned | Requires a future composition adapter. |

The model description reports artifact capabilities such as
`supportsModelExchange`; that flag describes what the FMU declares, not what
this Executor can run. An executable FMU must provide Co-Simulation. Parsing
and validation still inspect the full `modelDescription.xml`.

## Model description and variables

The normalized model description includes model identity and capabilities,
default experiment values, and variable metadata when declared:

- causality, variability, type, value reference and description;
- start and nominal values, bounds and inherited bounds from declared types;
- units, quantity, display units and conversion factor/offset;
- enumeration items and array dimensions.

The display-unit conversion includes the inverse conversion when defined. FMI
`min` and `max` describe model limits; FMI does not define a control resolution.

`GET /internal/fmu/describe?publicOnly=true` omits variables with `local`
causality before constructing the response. Other non-local causalities remain
available, including input, output, parameter and independent. The optional
`causality=output` filter returns outputs only. The WebSocket
`model.describe` request accepts `includeVariables: false` to omit the variable
list when a caller only needs model-level metadata.

These filters control the description payload; they do not change simulation
or bypass Gateway AAS permissions. The Gateway controls which variables a
public caller may read or write.

## Initialization and runtime writes

At initialization, values are validated against declared variable names,
causality, types, ranges and array lengths. Initial values can be supplied for
FMU inputs and non-constant parameters. In a realtime session the preferred
location is `sim.initialize.options.inputs`; the top-level `parameters` field
is also accepted for compatibility with the internal request contract.

During execution, writes are allowed to declared `input` variables. FMI 2 and
FMI 3 Co-Simulation also allow runtime writes to variables declared with
`causality="parameter"` and `variability="tunable"`. Other parameter
variabilities are not treated as runtime controls. Each write is validated
before the FMU call.

Synchronous `run` and `stream` requests can carry `inputSchedule` events.
Eligible inputs and tunable parameters are applied at communication points.
Asynchronous jobs and batches do not accept input schedules. Event count,
payload size, and the other request limits are configurable in the
[configuration reference](configuration.md). Scheduled changes do not turn
fixed parameters into tunable ones.

The FMI proxy uses `sim.reset` with `deferInitialization: true` when it needs
to create a fresh FMU instance and then initialize it with new experiment
options or initial inputs.

## Simulation clock and stepping

`sim.initialize.options.timeMode` selects the realtime session clock:

| Mode | Behavior |
|---|---|
| `simtime` | Automatic sessions advance as fast as the FMU can run. |
| `realtime` | Model time is paced against wall-clock time using `playbackSpeed` (default `1`, allowed range `0.1–100`). |
| `external` | Automatic start, resume and `runUntil` are disabled. The client advances with `sim.step` or `sim.advance` and chooses each communication point. |

Start time, stop time and step size come from the request or the FMU's declared
default experiment where available. Requests must have a finite stop time
greater than the start time and a positive step size. The configured maximum
step budget still applies.

FMI 3 Event Mode is negotiated with the FMU and handled inside the isolated
Executor before returning a step. Step responses include the last successful
simulation time, early-return and termination flags, and whether an event
occurred. `sim.advance` returns a bounded set of intermediate samples in one
request; see the [WebSocket API reference](api.md#realtime-websocket-protocol).
In `external` mode, `sim.advance` may not target a time beyond `stopTime`.
`inputValues` applies one write set at the current communication point before
the advance, without requiring it to align to the model's default step grid.
Apps with high-rate timed inputs can send a bounded `inputSchedule` with each
advance and use `outputSampleEverySteps` to keep integration steps separate
from the rate at which results cross the WebSocket.

Automatic progress messages are throttled independently of `stepSize`.
Realtime output reads share the configured per-session rate cap across output
subscriptions, direct `sim.getOutputs` calls, and output responses from
`sim.step`, `sim.advance` and `sim.runUntil`. A subscription may request a lower `periodMs` or
`maxHz`, but cannot exceed the configured cap. A read that arrives too soon
returns a retryable `OUTPUT_READ_RATE_LIMITED` error with `retryAfterMs`. After
that delay, retry through Gateway with a new `requestId`; Gateway replays
responses for request IDs it has already seen.

## Admission, quotas and reservation expiry

Realtime sessions, synchronous runs, streams, jobs, batches and validation
probes reserve from the same concurrent execution capacity. Job admission is
immediate. When capacity is full, a request returns
`STATION_CAPACITY_EXHAUSTED` (HTTP 429) without debiting the run quota. A job
reported as `queued` has been admitted and its worker is starting; it is not
waiting in a queue.

Gateway execution context carries two independent deadlines:

- `executionAuthorizationExpiresAt`, bounded by the signed authorization's
  `exp`;
- `reservationEndsAt`, the end of the booking.

The Executor checks both deadlines before batch cases and while workers run. It
terminates work at the first deadline and records `authorization_expired`
separately from `reservation_ended`. The short-lived WebSocket ticket does not
define the reservation end.

The signed Gateway claims may include `fmuExecutionPolicy`. Executor accepts
`maxDailyScenarios`, `maxSteps`, `maxCpuSeconds`, `maxMemoryMiB`,
`maxResultBytes` and `maxInputUpdatesPerWindow`; values are validated against
Station hard limits. The per-resource input-update limit can be set by the
trusted ticket issuer and cannot exceed `FMU_MAX_INPUT_UPDATES_PER_WINDOW`.
Omitted values use Station defaults.

Scenario starts and runtime input updates use separate budgets. By default,
scenario starts are limited per reservation and FMU per UTC day. Realtime
input updates are tracked by reservation and FMU with daily count and byte
budgets, a short-window burst limit, and a per-update payload cap. The default
short-window burst uses the Station maximum; a signed per-resource policy may
lower it. A non-empty
`sim.setInputs` requires a reservation scope (`labId` and `reservationKey`) and
does not spend a simulation scenario. All exact defaults and allowed ranges
are in the [configuration reference](configuration.md).

Each batch scenario's parameter JSON is limited to 16 KiB. The total number of
cases is bounded by `FMU_MAX_BATCH_CASES`. A batch may use at most twice the
configured per-initialization step limit across its scenarios. Direct legacy
`run` and `stream` calls made with only the internal token do not consume
reservation scenario quota or create job history.

## Worker isolation and resource limits

One-shot, NDJSON streaming and realtime sessions run in spawned worker
processes. The API process retains the session registry and sends an allowlisted
control set over private IPC. This scopes native FMU crashes and blocking calls
to one session while allowing reconnects during the configured grace period.
`FMU_EXECUTION_MODE=in-process` is intended only for diagnostics or environments
that provide isolation elsewhere.

Workers receive private temporary directories, and inherited environment
variables with credential-like names are removed. Platform controls include:

- **Linux:** CPU, memory, per-file and descriptor limits; Landlock write
  restrictions and read access limited to the Python/system runtime and pinned
  FMU; seccomp denies network sockets and child-process execution by default.
  A narrowly allowlisted FMU may opt into the configured Modelica Spawn
  executable; Landlock then permits only that binary, its dynamic loader and
  the `/bin/sh` invocation required by the adapter. These controls fail closed
  when unavailable. See [Linux Spawn configuration](configuration.md#optional-linux-spawn-executable).
- **Windows:** per-worker AppContainers without network capabilities, with file
  access granted to the bundled runtime and private work directory. Job Objects
  enforce CPU time, memory, child-process count and complete process-tree
  termination.

Windows workers run under the sidecar account, normally `SYSTEM`. Windows does
not currently enforce a per-file or aggregate temporary-disk quota. Startup
cleans abandoned worker directories and AppContainer profiles. The
`workerIsolation` value in validation responses reports the controls available
on the current Station.

The Docker image defaults to a dedicated non-root user. The local Docker
profile adds a read-only root filesystem, resource limits, a process cap and a
bounded `/tmp` tmpfs; see [configuration](configuration.md#container-image).

## FMU validation and quarantine

Validation separates four outcomes: `archiveValid`, `modelDescriptionValid`,
`stationCompatible` and `executionChecked`. It checks compressed and expanded
archive bounds, path traversal and duplicate paths, FMI version and
capabilities, GUID, the exact Co-Simulation binary for the current operating
system and architecture, and whether native dependency loading was checked.

Use `POST /internal/fmu/validate/{access_key}?probe=true` to load and initialize
an FMU in an isolated worker. A passing probe checks the dependencies required
for initialization; it does not certify every possible simulation path.
`auto_quarantine` can request quarantine when validation finds an invalid
artifact. If the probe cannot run because worker isolation infrastructure is
unavailable, automatic quarantine is skipped. Manual quarantine and removal
are available through the internal API.

## Reproducibility and stored results

Accepted runs retain a read-only, content-addressed FMU snapshot for each case
in a batch. Evidence records the snapshot SHA-256, Executor version and commit,
FMPy and platform versions, effective parameters/options/policy, finish reason,
warnings and result SHA-256. This evidence describes what the provider-side
Executor ran; it is not an independent institutional attestation.

HTTP run, stream, job and batch requests may select `outputVariables` and
reduce output volume with `outputSampleEverySteps` (default `1`, meaning no
additional sampling). Per-result and total
history storage limits remain enforced after selection and sampling. History
retention, result-size and aggregate storage defaults are listed in
[configuration](configuration.md#request-simulation-and-retention-limits).

## Planned composition backend

OMSimulator is optional and is not a dependency of the Windows Station runtime.
Ordinary single-FMU requests use FMPy. `options.backend: "omsimulator"` is
reserved for a possible future SSP/multi-FMU composition adapter; until that
adapter exists, such a request returns HTTP `501`. Backend diagnostics report
whether the executable is installed separately from whether Executor can run
it. Until implementation, `available` and `executable` remain false and
`supports` remains empty.
