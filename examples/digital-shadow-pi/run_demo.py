"""Run a StateSpace FMU with an external PI through FMU Executor's WebSocket."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

from websockets.asyncio.client import connect


REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_FMUS = REPO_ROOT / "examples" / "reference-fmus"
DEFAULT_STEP_SIZE = 0.1
DEFAULT_DURATION = 10.0
DEFAULT_SETPOINT = 0.6
DEFAULT_KP = 1.0
DEFAULT_KI = 0.2
STATE_SPACE_A = -0.2
STATE_SPACE_B = 0.2
STATE_SPACE_INITIAL_OUTPUT = 0.0


class ExecutorClient:
    def __init__(self, websocket):
        self.websocket = websocket

    async def request(self, message: dict, expected_type: str) -> dict:
        request_id = uuid4().hex
        await self.websocket.send(
            json.dumps({**message, "requestId": request_id}, allow_nan=False)
        )
        while True:
            response = json.loads(await self.websocket.recv())
            if response.get("requestId") != request_id:
                continue
            if response.get("type") == "error":
                raise RuntimeError(
                    f"FMU-Executor respondió {response.get('code')}: "
                    f"{response.get('message')}"
                )
            if response.get("type") != expected_type:
                raise RuntimeError(
                    f"Respuesta inesperada: se esperaba {expected_type}, "
                    f"se recibió {response.get('type')}"
                )
            return response


class PIController:
    def __init__(
        self,
        *,
        setpoint: float,
        kp: float,
        ki: float,
        lower: float = 0.0,
        upper: float = 1.0,
        initial_input: float = 0.0,
        direction: int = 1,
    ):
        self.setpoint = setpoint
        self.kp = kp
        self.ki = ki
        self.lower = lower
        self.upper = upper
        self.direction = direction
        self.integral = initial_input

    def update(self, measured_y: float, step_size: float) -> float:
        error = self.direction * (self.setpoint - measured_y)
        candidate_integral = self.integral + self.ki * error * step_size
        raw_u = self.kp * error + candidate_integral
        control_u = min(self.upper, max(self.lower, raw_u))
        if (
            raw_u == control_u
            or (raw_u > self.upper and error < 0.0)
            or (raw_u < self.lower and error > 0.0)
        ):
            self.integral = candidate_integral
        return control_u


def local_gateway_context(access_key: str) -> dict:
    """Build a clearly local-only reservation context for the private API."""
    subject = "digital-shadow-pi-fixture"
    lab_id = "local-digital-shadow-fixture"
    reservation_key = f"local-{uuid4().hex}"
    claims = {
        "accessKey": access_key,
        "sub": subject,
        "labId": lab_id,
        "reservationKey": reservation_key,
        "exp": time.time() + 900,
    }
    return {
        "accessKey": access_key,
        "sub": subject,
        "labId": lab_id,
        "reservationKey": reservation_key,
        "claims": claims,
    }


async def create_session(client: ExecutorClient, access_key: str) -> str:
    response = await client.request(
        {
            "type": "session.create",
            "gatewayContext": local_gateway_context(access_key),
        },
        "session.created",
    )
    return response["sessionId"]


async def terminate_session(client: ExecutorClient, session_id: str | None) -> None:
    if session_id is None:
        return
    try:
        await client.request({"type": "session.terminate"}, "session.closed")
    except Exception:
        # The enclosing WebSocket and temporary Executor still close on errors.
        pass


async def run_feedthrough(client: ExecutorClient, value: float) -> None:
    session_id = None
    try:
        session_id = await create_session(client, "Feedthrough.fmu")
        await client.request(
            {
                "type": "sim.initialize",
                "options": {
                    "executionInterface": "CoSimulation",
                    "timeMode": "external",
                    "startTime": 0.0,
                    "stopTime": 0.1,
                    "stepSize": 0.1,
                    "inputs": {"Float64_continuous_input": value},
                },
            },
            "sim.state",
        )
        response = await client.request(
            {
                "type": "sim.advance",
                "currentCommunicationPoint": 0.0,
                "targetTime": 0.1,
                "stepSize": 0.1,
                "inputValues": {"Float64_continuous_input": value},
                "outputVariables": ["Float64_continuous_output"],
                "maxSamples": 1,
            },
            "sim.samples",
        )
        samples = response.get("samples", [])
        if not samples:
            raise RuntimeError("Feedthrough no devolvió muestras")
        echoed_value = samples[-1]["outputs"]["Float64_continuous_output"]
        if not math.isclose(float(echoed_value), value, rel_tol=1e-9, abs_tol=1e-9):
            raise RuntimeError(
                f"Feedthrough devolvió {echoed_value}; se esperaba {value}"
            )
        print(f"Feedthrough: u={value:.3f} -> y={float(echoed_value):.3f} (OK)")
    finally:
        await terminate_session(client, session_id)


async def initialize_state_space(
    client: ExecutorClient,
    *,
    duration: float,
    step_size: float,
) -> dict:
    return await client.request(
        {
            "type": "sim.initialize",
            "options": {
                "executionInterface": "CoSimulation",
                "timeMode": "external",
                "startTime": 0.0,
                "stopTime": duration,
                "stepSize": step_size,
                "inputs": {"u": [0.0]},
            },
            "parameters": {
                "m": 1,
                "n": 1,
                "r": 1,
                "A": [STATE_SPACE_A],
                "B": [STATE_SPACE_B],
                "C": [1.0],
                "D": [0.0],
                "x0": [STATE_SPACE_INITIAL_OUTPUT],
            },
        },
        "sim.state",
    )


async def advance_state_space(
    client: ExecutorClient,
    *,
    model_time: float,
    target_time: float,
    step_size: float,
    control_u: float,
) -> dict:
    return await client.request(
        {
            "type": "sim.advance",
            "currentCommunicationPoint": model_time,
            "targetTime": target_time,
            "stepSize": step_size,
            "inputValues": {"u": [control_u]},
            "outputVariables": ["y"],
            "maxSamples": 1,
        },
        "sim.samples",
    )


def output_interval_seconds(initialization: dict) -> float:
    max_output_hz = int(initialization.get("capabilities", {}).get("maxOutputHz", 60))
    return 1.0 / max(1, max_output_hz) + 0.005


async def run_pi(
    client: ExecutorClient,
    *,
    setpoint: float,
    duration: float,
    step_size: float,
    kp: float,
    ki: float,
) -> list[dict[str, float]]:
    session_id = None
    rows: list[dict[str, float]] = []
    try:
        session_id = await create_session(client, "StateSpace.fmu")
        initialized = await initialize_state_space(
            client, duration=duration, step_size=step_size
        )

        simulated_time = 0.0
        measured_y = STATE_SPACE_INITIAL_OUTPUT
        controller = PIController(setpoint=setpoint, kp=kp, ki=ki)
        last_output_read: float | None = None
        output_interval = output_interval_seconds(initialized)

        print("\nStateSpace + PI externo")
        print(" tiempo_s  referencia       u       y   error")

        while simulated_time < duration - 1e-12:
            control_u = controller.update(measured_y, step_size)
            target_time = min(duration, simulated_time + step_size)
            if last_output_read is not None:
                wait_seconds = output_interval - (time.monotonic() - last_output_read)
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)

            response = await advance_state_space(
                client,
                model_time=simulated_time,
                target_time=target_time,
                step_size=step_size,
                control_u=control_u,
            )
            last_output_read = time.monotonic()
            samples = response.get("samples", [])
            if not samples:
                raise RuntimeError("StateSpace no devolvió muestras")
            sample = samples[-1]
            simulated_time = float(sample["time"])
            measured_y = float(sample["outputs"]["y"][0])
            if not math.isfinite(measured_y):
                raise RuntimeError("StateSpace devolvió una salida no finita")

            row = {
                "time_s": simulated_time,
                "setpoint": setpoint,
                "u": control_u,
                "y": measured_y,
                "error": setpoint - measured_y,
            }
            rows.append(row)
            print(
                f"{simulated_time:9.2f}  {setpoint:10.3f}  "
                f"{control_u:6.3f}  {measured_y:6.3f}  {row['error']:6.3f}"
            )

        print(
            f"Resultado final: y={measured_y:.4f}, "
            f"error={setpoint - measured_y:.4f}, muestras={len(rows)}"
        )
        return rows
    finally:
        await terminate_session(client, session_id)


def write_csv(path: Path, rows: list[dict[str, float]]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=["time_s", "setpoint", "u", "y", "error"],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"CSV guardado en {path}")


def reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def copy_reference_fmus(data_root: Path) -> None:
    data_root.mkdir(parents=True, exist_ok=True)
    for filename in ("StateSpace.fmu", "Feedthrough.fmu"):
        source = REFERENCE_FMUS / filename
        if not source.is_file():
            raise FileNotFoundError(f"No se encontró el FMU de referencia: {source}")
        shutil.copyfile(source, data_root / filename)


def wait_for_executor(process: subprocess.Popen, port: int, log_path: Path) -> None:
    deadline = time.monotonic() + 30.0
    health_url = f"http://127.0.0.1:{port}/internal/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"FMU-Executor terminó durante el arranque. Registro:\n"
                f"{log_path.read_text(encoding='utf-8', errors='replace')}"
            )
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as response:
                health = json.loads(response.read())
                if health.get("status") == "UP":
                    return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError(
        "FMU-Executor no estuvo listo en 30 segundos. Registro:\n"
        f"{log_path.read_text(encoding='utf-8', errors='replace')}"
    )


def stop_executor(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def connect_local_executor(port: int, token: str):
    websocket_url = f"ws://127.0.0.1:{port}/internal/fmu/sessions"
    return connect(
        websocket_url,
        additional_headers={"X-Internal-Session-Token": token},
        open_timeout=10,
        close_timeout=2,
    )


async def run_websocket_demo(
    port: int,
    token: str,
    args: argparse.Namespace,
) -> list[dict[str, float]]:
    async with connect_local_executor(port, token) as websocket:
        client = ExecutorClient(websocket)
        if args.mode in {"all", "feedthrough"}:
            await run_feedthrough(client, args.feedthrough_value)
        if args.mode in {"all", "pi"}:
            return await run_pi(
                client,
                setpoint=args.setpoint,
                duration=args.duration,
                step_size=args.step_size,
                kp=args.kp,
                ki=args.ki,
            )
    return []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ejecuta Feedthrough y un lazo PI externo por WebSocket."
    )
    parser.add_argument(
        "--mode",
        choices=("all", "feedthrough", "pi"),
        default="all",
        help="Prueba de señales, lazo PI o ambas (por defecto: all).",
    )
    parser.add_argument("--setpoint", type=float, default=DEFAULT_SETPOINT)
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    parser.add_argument("--step-size", type=float, default=DEFAULT_STEP_SIZE)
    parser.add_argument("--kp", type=float, default=DEFAULT_KP)
    parser.add_argument("--ki", type=float, default=DEFAULT_KI)
    parser.add_argument("--feedthrough-value", type=float, default=0.7)
    parser.add_argument(
        "--csv",
        type=Path,
        help="Opcionalmente guarda las muestras del lazo PI en un CSV.",
    )
    args = parser.parse_args()

    numeric_values = (
        args.setpoint,
        args.duration,
        args.step_size,
        args.kp,
        args.ki,
        args.feedthrough_value,
    )
    if not all(math.isfinite(value) for value in numeric_values):
        parser.error("los parámetros numéricos deben ser finitos")
    if not 0.0 <= args.setpoint <= 1.0:
        parser.error("--setpoint debe estar entre 0 y 1")
    if args.duration <= 0 or args.step_size <= 0:
        parser.error("--duration y --step-size deben ser positivos")
    if args.kp < 0 or args.ki < 0:
        parser.error("--kp y --ki no pueden ser negativos")
    if not 0.0 <= args.feedthrough_value <= 1.0:
        parser.error("--feedthrough-value debe estar entre 0 y 1")
    return args


@contextmanager
def local_executor(fmu: Path | None = None) -> Iterator[tuple[int, str]]:
    with tempfile.TemporaryDirectory(prefix="fmu-digital-shadow-pi-") as temporary:
        temporary_root = Path(temporary)
        fmu_root = temporary_root / "fmu-data"
        if fmu is None:
            copy_reference_fmus(fmu_root)
        else:
            fmu_root.mkdir()
            shutil.copyfile(fmu, fmu_root / fmu.name)

        port = reserve_local_port()
        token = token_urlsafe(32)
        environment = os.environ.copy()
        environment.pop("FMU_INTERNAL_TOKEN_B64", None)
        environment.pop("FMU_INTERNAL_TOKEN_FILE", None)
        environment.update(
            {
                "FMU_EXECUTOR_HOST": "127.0.0.1",
                "FMU_EXECUTOR_PORT": str(port),
                "FMU_INTERNAL_TOKEN": token,
                "FMU_EXECUTION_MODE": "process",
                "FMU_ROOT": str(fmu_root),
                "FMU_EXECUTOR_STATE_DIR": str(temporary_root / "state"),
                "FMU_EXECUTOR_TEMP": str(temporary_root / "tmp"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        existing_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(REPO_ROOT), existing_pythonpath) if value
        )

        log_path = temporary_root / "executor.log"
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]
        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=environment,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            try:
                wait_for_executor(process, port, log_path)
                yield port, token
            except Exception as exc:
                stop_executor(process)
                executor_log = log_path.read_text(encoding="utf-8", errors="replace").strip()
                if executor_log:
                    raise RuntimeError(f"{exc}\nRegistro de FMU-Executor:\n{executor_log}") from exc
                raise
            finally:
                stop_executor(process)


def main() -> int:
    args = parse_args()
    with local_executor() as (port, token):
        print("FMU-Executor local iniciado en 127.0.0.1")
        rows = asyncio.run(run_websocket_demo(port, token, args))

    if args.csv and rows:
        write_csv(args.csv, rows)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, TimeoutError, KeyError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
