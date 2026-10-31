# Configuration and installation

FMU Executor is an internal service used by Lab Gateway and the Lab Station
supervisor. It listens on a private Station interface and authenticates the
Gateway-to-Executor connection with a shared token. Do not expose the service
port to the public Internet.

## Install and run

The supported runtime requires Python 3.11 or newer.

```bash
python -m pip install -r requirements.txt
python -m app
```

The service binds to `0.0.0.0:8091` by default. Set a non-empty token in the
Station environment and configure the same value as Gateway's
`FMU_STATION_INTERNAL_TOKEN`. The Windows Lab Station supervisor uses the
configured Executor port for its health check and creates an inbound
`LabStation-FMU-Executor` firewall rule for Domain and Private network
profiles. Starting `python -m app` manually does not create that rule.

## Windows Lab Station

When Lab Station starts the sidecar through `LabStation\\BackgroundService`,
Windows Task Scheduler runs it as `SYSTEM`. A Python install or package set
scoped to an interactive user is not visible to that account. Install Python
machine-wide, or use a virtual environment under the Lab Station installation,
and make sure its interpreter and packages are readable by `SYSTEM`.

For example, verify the environment from a SYSTEM shell before starting the
task:

```powershell
psexec -accepteula -s -i powershell.exe
python -m pip install -r C:\LabStation\fmu-executor\requirements.txt
python -c "import uvicorn"
```

The service task must resolve the same interpreter and dependencies. Do not
rely on packages installed only for the administrator account.

The Lab Station **Connectors** panel reports the FMU endpoint, model directory,
port, token status and the Gateway environment expected for Station mode.

![Lab Station FMI/FMU connector](images/labstation-connectors-fmi.png)

*FMI/FMU connector view from the Lab Station desktop UI.*

## Environment variables

All settings are read from the Executor process environment. Bounded integer
settings are clamped to the documented range; an invalid integer falls back to
the default. Durations expressed in seconds are clamped to at least one second
unless otherwise stated.

### Service and storage

| Variable | Default | Description |
|---|---:|---|
| `FMU_EXECUTOR_HOST` | `0.0.0.0` | Bind address. |
| `FMU_EXECUTOR_PORT` | `8091` | TCP port; must be from 1 to 65535. |
| `FMU_ROOT` | `<repository>/fmu-data` | Root directory containing provisioned FMUs and their companion files. |
| `FMU_EXECUTOR_STATE_DIR` | `<FMU_ROOT parent>/state` | Persistent SQLite job history and quota store, plus private serialized FMI state checkpoints. Keep it persistent and writable only by the Executor service account. |
| `FMU_EXECUTOR_TEMP` | `<FMU_ROOT>/.tmp` | Temporary directory used while extracting and running FMUs. |
| `FMU_EXECUTION_MODE` | `process` | Worker boundary: `process` (recommended) or `in-process` (diagnostics only). Any other value selects `process`. |
| `FMU_EXECUTION_TIMEOUT_SECONDS` | `3600` | Maximum duration for a one-shot or streaming worker; minimum `1`. |
| `FMU_MAX_SESSIONS` | `4` | Shared concurrent capacity for sessions, runs, streams, jobs, batches and validation probes. |
| `FMU_ATTACH_GRACE_SECONDS` | `120` | Seconds a detached realtime session can be reattached before its FMU state is terminated; minimum `0`. |
| `FMU_LOG_LEVEL` | `INFO` | Python logging level. |
| `FMU_EXECUTOR_COMMIT` | `unknown` | Build-provided source commit recorded in execution evidence. |

The Windows Lab Station service sets `FMU_EXECUTOR_STATE_DIR` to
`%ProgramData%\DecentraLabs\Lab Station\fmu-executor-state` and restricts
that directory to `SYSTEM` and local Administrators. If the service account or
deployment is customized, keep this directory persistent and writable only by
the Executor service identity.

FMUs can be stored directly under `FMU_ROOT` or under an access-key directory:

```text
fmu-data/
  Heater.fmu             # access key: Heater.fmu
  my-model/
    model.fmu             # access key: my-model
```

The included model files and their platform coverage are listed in the
[Reference FMUs](../examples/reference-fmus/README.md) and
[Smart Warehouse Cell](../examples/smart-warehouse-cell/README.md) guides.

### Internal authentication

| Variable | Default | Description |
|---|---|---|
| `FMU_INTERNAL_TOKEN_B64` | *(unset)* | Base64 or URL-safe Base64 encoding of the UTF-8 shared token. If set, it takes precedence over the token file and direct value. |
| `FMU_INTERNAL_TOKEN_FILE` | *(unset)* | File containing the shared token; surrounding whitespace is stripped. Used when the Base64 setting is unset. |
| `FMU_INTERNAL_TOKEN` | *(unset)* | Direct shared token value, used when the other token settings are unset. Requests fail closed if no usable token is configured. |

The lookup order is `FMU_INTERNAL_TOKEN_B64`, then
`FMU_INTERNAL_TOKEN_FILE`, then `FMU_INTERNAL_TOKEN`. A configured but invalid
Base64 value or unreadable token file does not fall back to the next setting.
Keep the token out of source control and public URLs. For a Windows scheduled
task, a machine-level variable can be set before the service starts:

```powershell
[Environment]::SetEnvironmentVariable('FMU_INTERNAL_TOKEN', '<random-shared-secret>', 'Machine')
```

Restart `LabStation\BackgroundService` after changing the token. Copy the same
secret to Gateway's `FMU_STATION_INTERNAL_TOKEN`.

### Request, simulation and retention limits

| Variable | Default | Allowed range | Applies to |
|---|---:|---:|---|
| `FMU_MAX_BATCH_CASES` | `8` | `1–20` | Scenarios in one batch. |
| `FMU_MAX_SCENARIOS_PER_RESERVATION_PER_DAY` | Unlimited | `1–10,000` | Optional scenario-start quota per reservation and FMU per UTC day. |
| `FMU_MAX_SIMULATION_PARAMETERS` | `32` | `1–256` | Named parameters in a simulation request. |
| `FMU_MAX_SIMULATION_STEPS` | `10,000` | `100–1,000,000` | Communication steps per initialization. |
| `FMU_MAX_INPUT_SCHEDULE_EVENTS` | `128` | `1–200,000` | Events in an input schedule. |
| `FMU_MAX_INPUT_SCHEDULE_BYTES` | `16 KiB` | `1 KiB–16 MiB` | Serialized input schedule payload. |
| `FMU_MAX_INPUT_UPDATE_BYTES` | `65,536` | `1,024–1 MiB` | One `sim.setInputs` payload or one scheduled event's values. |
| `FMU_MAX_INPUT_UPDATES_PER_DAY` | Unlimited | `1–1,000,000` | Optional input-write quota per reservation and FMU per UTC day, including timed writes. |
| `FMU_MAX_INPUT_UPDATE_BYTES_PER_DAY` | Unlimited | `64 KiB–2 GiB` | Optional input-payload quota per reservation and FMU per UTC day. |
| `FMU_MAX_INPUT_UPDATES_PER_WINDOW` | Unlimited | `1–10,000` | Optional global sliding-window quota for input writes, including timed writes. A signed `fmuExecutionPolicy.maxInputUpdatesPerWindow` can impose a lower per-resource limit. |
| `FMU_INPUT_UPDATE_WINDOW_SECONDS` | `10` | `1–3,600` | Window duration used only when an input-write quota is configured. |
| `FMU_MAX_OUTPUT_SUBSCRIPTION_HZ` | `60` | `1–1,000` | Per-session cap for realtime output reads and samples. |
| `FMU_MAX_OUTPUT_SUBSCRIPTION_BATCH` | `256` | `1–4,096` | Retained samples in one subscription batch. |
| `FMU_PROGRESS_INTERVAL_MS` | `50` | `10–5,000` | Minimum interval between automatic progress messages. |
| `FMU_MAX_OUTPUT_SAMPLE_EVERY_STEPS` | `10,000` | `1–100,000` | Maximum configured stride for output sampling. |
| `FMU_HISTORY_RETENTION_DAYS` | `7` | `1–30` | Job history retention period. |
| `FMU_MAX_HISTORY_RECORDS` | `10,000` | `100–100,000` | Maximum stored history records. |
| `FMU_MAX_RESULT_BYTES` | `8 MiB` | `64 KiB–32 MiB` | Maximum size of one stored result. |
| `FMU_MAX_HISTORY_BYTES` | `256 MiB` | `16 MiB–2 GiB` | Maximum total stored result data. |

Each batch scenario also has a fixed 16 KiB parameter-JSON limit; it is not an
environment variable.

Quota variables accept a positive integer to enable an explicit cap. An empty
value, `0`, or `unlimited` leaves the quota unlimited. Request-size,
per-advance, simulation-step, worker-resource, and reservation-deadline limits
remain enforced independently.

### FMU archive and worker limits

| Variable | Default | Allowed range | Applies to |
|---|---:|---:|---|
| `FMU_MAX_ARCHIVE_BYTES` | `512 MiB` | `1 KiB–4 GiB` | Compressed FMU archive size. |
| `FMU_MAX_EXPANDED_BYTES` | `2 GiB` | `1 KiB–16 GiB` | Total expanded archive entry size. |
| `FMU_MAX_ARCHIVE_ENTRIES` | `20,000` | `1–200,000` | ZIP entry count. |
| `FMU_MAX_MODEL_DESCRIPTION_BYTES` | `32 MiB` | `1 KiB–128 MiB` | `modelDescription.xml` size. |
| `FMU_WORKER_CPU_SECONDS` | `300` | `1–86,400` | Per-worker CPU time limit. |
| `FMU_WORKER_MEMORY_MIB` | `1,024 MiB` | `128–16,384 MiB` | Per-worker memory limit. |
| `FMU_WORKER_FILE_MIB` | `128 MiB` | `1–4,096 MiB` | Linux per-file write limit. Windows AppContainer workers do not have this per-file quota. |

### Optional OMSimulator discovery

These settings only support discovery of a possible future composition
backend. They do not enable OMSimulator execution today.

| Variable | Default | Description |
|---|---:|---|
| `FMU_OMSIMULATOR_ENABLED` | `false` | Enables executable discovery. Accepts `1`, `true`, `yes` or `on`. |
| `FMU_OMSIMULATOR_COMMAND` | `OMSimulator` | Executable path or command name to inspect. |
| `FMU_OMSIMULATOR_TIMEOUT_SECONDS` | `3600` | Reserved adapter timeout; minimum `1`. |

### Optional Linux Spawn executable

Some FMUs need the Modelica Spawn executable at runtime. This opt-in is
available only to explicitly allowlisted FMU access keys on Linux. Keep the
allowlist as narrow as possible; it does not enable general child-process
execution.

| Variable | Default | Description |
|---|---|---|
| `FMU_SPAWN_ACCESS_KEYS` | *(unset)* | Comma-separated exact FMU access keys allowed to use Spawn. |
| `FMU_SPAWN_EXECUTABLE` | *(unset)* | Absolute path to the Spawn executable. The file must exist, be executable and be directly inside `SPAWNPATH`. |
| `SPAWNPATH` | *(unset)* | Absolute path to the directory containing `FMU_SPAWN_EXECUTABLE`. Required when a whitelisted key is used. |

If a key is allowlisted but the executable or directory is missing, the worker
fails closed. Linux isolation then permits only the configured Spawn binary,
its dynamic loader and the `/bin/sh` invocation used by the Modelica adapter;
the FMU worker remains restricted by its filesystem and resource limits.

## Container image

The repository `Dockerfile` builds the optional Executor image used by Lab
Gateway's local Executor profile. It uses the Docker Official Python 3.11 slim
image from Amazon ECR Public. A `vX.Y.Z` tag publishes
`ghcr.io/decentralabscom/fmu-executor:X.Y.Z`; the tag must match `VERSION` and
the package version in `pyproject.toml`. The publishing workflow builds the
`linux/amd64` image and includes OCI source, version and commit labels. The
Gateway pins a versioned image by default, so a Gateway installation does not
need an FMU Executor source checkout.

The image defaults to a dedicated non-root user. The local Docker profile adds
a read-only root filesystem, resource limits, a process cap and a bounded `/tmp`
tmpfs. If an installation must pull the image without GitHub authentication,
the GHCR package must have public visibility.
