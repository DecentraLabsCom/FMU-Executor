# FMU Executor

FMU Executor is the private simulation service packaged with DecentraLabs Lab
Station. It loads and runs FMI 2.0 and FMI 3.0 Co-Simulation FMUs on Windows or
Linux. Lab Gateway authorizes reservation-bound work and forwards it to the
Station; FMU Executor runs the model and returns results. It is not a public
API and should only be reachable by trusted Gateway and Station components.

```text
Marketplace → Lab Gateway → Lab Station → FMU Executor → FMU
```

The service is written in Python and requires Python 3.11 or newer.

## Start locally

```bash
python -m pip install -r requirements.txt
python -m app
```

To install the Python package instead, use `python -m pip install .`.

By default it listens on `0.0.0.0:8091`. Before using it with Lab Gateway,
configure a non-empty shared internal token in the Station and set the matching
`FMU_STATION_INTERNAL_TOKEN` in Gateway. Keep the Executor port private to that
connection.

## Documentation

| Guide | Contents |
|---|---|
| [Configuration and installation](docs/configuration.md) | Runtime settings, storage, Windows service context, tokens and container releases |
| [Private API](docs/api.md) | Internal HTTP and WebSocket contracts used by Lab Gateway |
| [Runtime and FMI support](docs/runtime.md) | FMI behavior, variable metadata, execution lifecycle, isolation, limits and validation |
| [Reference FMUs](examples/reference-fmus/README.md) | Included reference models, provenance, licenses and supported platforms |
| [Smart Warehouse Cell](examples/smart-warehouse-cell/README.md) | Included visual FMI example, assets and build instructions |

These guides are the source of truth for FMU Executor behavior. Lab Gateway's
public reservation, proxy delivery, AAS policy and browser visualization flows
are described in the [Gateway FMI/FMU guide](https://github.com/DecentraLabsCom/Lab-Gateway/blob/main/docs/fmi-fmu-support.md).

## Development checks

From the repository root:

```bash
python -m pip install pytest httpx
python -m pytest tests/ -v
```

## Releases

`VERSION` is the source release version. Keep it aligned with the package
version in `pyproject.toml` and the release snapshots carried by the Windows and
Linux Lab Station repositories. The `Dockerfile` builds the optional Gateway
Executor image. Pushing a matching `vX.Y.Z` tag publishes
`ghcr.io/decentralabscom/fmu-executor:X.Y.Z`; Gateway pins a versioned image by
default, so its local Executor profile does not need a source checkout.
