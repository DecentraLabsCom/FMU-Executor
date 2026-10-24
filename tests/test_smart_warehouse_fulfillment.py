"""Model and scene checks for the Smart Warehouse order fulfillment cycle."""

from pathlib import Path
import json
import struct
import sys
import zipfile

import numpy as np
import pytest
from fmpy import read_model_description, simulate_fmu


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = PROJECT_ROOT / "examples" / "smart-warehouse-cell"
FMU_PATH = EXAMPLE_ROOT / (
    "SmartWarehouseCell.fmu" if sys.platform == "win32" else "SmartWarehouseCell-linux64.fmu"
)
SCENE_PATH = EXAMPLE_ROOT / "smart-warehouse-cell.glb"
STEP_SIZE = 0.05
AGV_ROUTE_LENGTH = 9.65
OUTPUTS = [
    "agvPosition",
    "robotAngle",
    "packageOnAgv",
    "alarm",
    "ordersPicked",
    "forkHeight",
    "inventoryLevel",
    "truckLoadCount",
    "orderComplete",
    "sourcePackageVisible",
    "pickupPackageVisible",
    "transferPackageVisible",
    "pickupProgress",
    "transferPackageX",
    "transferPackageY",
    "transferPackageZ",
    "loaderAngle",
    "loaderReach",
]


def simulate(*, order_quantity=6, speed=0.68, rush_order=False, jam=False, stop_time=250):
    inputs = np.array(
        [
            (0.0, speed, True, jam, rush_order, order_quantity),
            (stop_time, speed, True, jam, rush_order, order_quantity),
        ],
        dtype=[
            ("time", np.float64),
            ("robotSpeed", np.float64),
            ("startStop", np.bool_),
            ("jam", np.bool_),
            ("rushOrder", np.bool_),
            ("orderQuantity", np.int32),
        ],
    )
    return simulate_fmu(
        str(FMU_PATH),
        start_time=0,
        stop_time=stop_time,
        step_size=STEP_SIZE,
        # FMPy uses output_interval as the Co-Simulation communication step.
        output_interval=STEP_SIZE,
        input=inputs,
        output=OUTPUTS,
    )


@pytest.mark.skipif(not FMU_PATH.is_file(), reason="Built Smart Warehouse FMU is unavailable")
def test_default_order_loads_six_packages_then_stops_at_the_truck():
    result = simulate()

    np.testing.assert_allclose(np.diff(result["time"]), STEP_SIZE, atol=1e-10)
    assert int(result["truckLoadCount"][-1]) == 6
    assert int(result["ordersPicked"][-1]) == 1
    assert bool(result["orderComplete"][-1])
    assert result["inventoryLevel"][-1] == pytest.approx(76)
    assert result["agvPosition"][-1] == pytest.approx(AGV_ROUTE_LENGTH)
    assert int(result["truckLoadCount"].max()) == 6
    assert np.allclose(result["agvPosition"][result["orderComplete"]], AGV_ROUTE_LENGTH)


@pytest.mark.skipif(not FMU_PATH.is_file(), reason="Built Smart Warehouse FMU is unavailable")
@pytest.mark.parametrize("order_quantity", [1, 3])
def test_requested_order_quantity_completes_without_overfilling_the_truck(order_quantity):
    result = simulate(order_quantity=order_quantity)

    assert int(result["truckLoadCount"][-1]) == order_quantity
    assert int(result["ordersPicked"][-1]) == 1
    assert bool(result["orderComplete"][-1])
    assert int(result["truckLoadCount"].max()) == order_quantity
    assert result["inventoryLevel"][-1] == pytest.approx(100 - 4 * order_quantity)
    assert result["agvPosition"][-1] == pytest.approx(AGV_ROUTE_LENGTH)


@pytest.mark.skipif(not FMU_PATH.is_file(), reason="Built Smart Warehouse FMU is unavailable")
def test_packages_are_picked_transferred_and_agv_returns_for_the_next_pick():
    # The second parcel uses a side slot, exercising loader rotation as well
    # as extension; the first parcel goes into the center slot without turning.
    result = simulate(order_quantity=3, stop_time=90)

    assert np.any(result["pickupPackageVisible"])
    assert np.any((result["pickupProgress"] > 0.2) & (result["pickupProgress"] < 0.9))
    assert np.any(result["transferPackageVisible"])
    assert np.max(result["transferPackageZ"][result["transferPackageVisible"]]) > 5.2
    assert np.min(result["loaderAngle"]) < -0.2 or np.max(result["loaderAngle"]) > 0.2
    assert result["loaderReach"].max() > 2.0
    assert result["agvPosition"].max() == pytest.approx(AGV_ROUTE_LENGTH)
    deliveries = np.flatnonzero(np.diff(result["truckLoadCount"]) > 0) + 1
    assert len(deliveries) == 2
    between_deliveries = result["agvPosition"][deliveries[0]:deliveries[1]]
    assert np.any(np.diff(between_deliveries) < 0)
    assert between_deliveries.min() == pytest.approx(0)
    assert np.any(np.diff(between_deliveries) > 0)
    assert int(result["truckLoadCount"][-1]) == 2
    assert int(result["ordersPicked"][-1]) == 0


@pytest.mark.skipif(not FMU_PATH.is_file(), reason="Built Smart Warehouse FMU is unavailable")
def test_jam_freezes_the_cycle_and_raises_the_alarm():
    result = simulate(jam=True, stop_time=40)

    assert np.all(result["alarm"][1:])
    assert np.allclose(result["agvPosition"], 0)
    assert int(result["truckLoadCount"][-1]) == 0
    assert not np.any(result["transferPackageVisible"])


@pytest.mark.skipif(not FMU_PATH.is_file(), reason="Built Smart Warehouse FMU is unavailable")
def test_rush_order_speeds_up_only_the_next_outbound_delivery():
    normal = simulate(order_quantity=2, speed=0.68, stop_time=100)
    rush = simulate(order_quantity=2, speed=0.68, rush_order=True, stop_time=100)

    normal_deliveries = normal["time"][np.diff(np.r_[0, normal["truckLoadCount"]]) > 0]
    rush_deliveries = rush["time"][np.diff(np.r_[0, rush["truckLoadCount"]]) > 0]
    assert len(normal_deliveries) == 2
    assert len(rush_deliveries) == 2
    assert rush_deliveries[0] < normal_deliveries[0] - 2.0
    assert rush_deliveries[1] - rush_deliveries[0] == pytest.approx(
        normal_deliveries[1] - normal_deliveries[0], abs=2 * STEP_SIZE
    )


@pytest.mark.skipif(not SCENE_PATH.is_file(), reason="Generated Smart Warehouse GLB is unavailable")
def test_scene_binds_pickup_conveyor_loader_and_six_truck_slots():
    content = SCENE_PATH.read_bytes()
    magic, version, total_length = struct.unpack_from("<4sII", content)
    chunk_length, chunk_type = struct.unpack_from("<II", content, 12)
    assert magic == b"glTF" and version == 2 and total_length == len(content)
    assert chunk_type == 0x4E4F534A
    document = json.loads(content[20 : 20 + chunk_length].decode("utf-8").rstrip(" \t\r\n\0"))
    bindings = [
        binding
        for node in document["nodes"]
        for binding in node.get("extras", {}).get("fmiBindings", [])
    ]
    # Procedural GLB nodes can be unnamed. The FMI bindings are the viewer's
    # public contract, independent of internal object names and hierarchy.
    required_bindings = {
        ("agvPosition", "position.x"),
        ("forkHeight", "position.y"),
        ("pickupArmX", "position.x"),
        ("pickupArmY", "position.y"),
        ("pickupArmZ", "position.z"),
        ("pickupDropProgress", "position.y"),
        ("pickupArmReachProgress", "scale.z"),
        ("pickupGripperGap", "position.x"),
        ("pickupPackageVisible", "visible"),
        ("packageOnAgv", "visible"),
        ("trayPackageX", "position.x"),
        ("trayBeltPhase", "rotation.y"),
        ("beltPhase", "rotation.y"),
        ("transferPackageVisible", "visible"),
        ("transferPackageX", "position.x"),
        ("transferPackageY", "position.y"),
        ("transferPackageZ", "position.z"),
        ("loaderAngle", "rotation.y"),
        ("loaderReach", "scale.z"),
        ("alarm", "material.color"),
    }
    required_bindings.update((f"shelfPackage{index}Visible", "visible") for index in range(1, 7))
    assert required_bindings <= {(b["variable"], b["target"]) for b in bindings}

    cargo_bindings = [b for b in bindings if b["variable"] == "truckLoadCount" and b["target"] == "visible"]
    assert len(cargo_bindings) == 6
    thresholds = {
        binding["visibleWhenGreaterThanOrEqual"]
        for binding in cargo_bindings
    }
    assert thresholds == {1, 2, 3, 4, 5, 6}


@pytest.mark.skipif(not FMU_PATH.is_file() or not SCENE_PATH.is_file(), reason="Built Smart Warehouse assets are unavailable")
def test_fmu_embeds_the_companion_scene_and_declares_all_bound_outputs():
    with zipfile.ZipFile(FMU_PATH) as archive:
        assert archive.read("resources/smart-warehouse-cell.glb") == SCENE_PATH.read_bytes()
    content = SCENE_PATH.read_bytes()
    chunk_length = struct.unpack_from("<I", content, 12)[0]
    document = json.loads(content[20:20 + chunk_length])
    bound_outputs = {
        binding["variable"]
        for node in document["nodes"]
        for binding in node.get("extras", {}).get("fmiBindings", [])
    }
    description = read_model_description(str(FMU_PATH))
    declared_outputs = {variable.name for variable in description.modelVariables if variable.causality == "output"}
    assert bound_outputs <= declared_outputs
