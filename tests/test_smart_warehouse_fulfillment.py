"""Model and scene checks for the Smart Warehouse order fulfillment cycle."""

from pathlib import Path
import json
import struct
import sys

import numpy as np
import pytest
from fmpy import simulate_fmu


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = PROJECT_ROOT / "examples" / "smart-warehouse-cell"
FMU_PATH = EXAMPLE_ROOT / (
    "SmartWarehouseCell.fmu" if sys.platform == "win32" else "SmartWarehouseCell-linux64.fmu"
)
SCENE_PATH = EXAMPLE_ROOT / "smart-warehouse-cell.glb"
STEP_SIZE = 0.05
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
]


def simulate(*, order_quantity=6, speed=0.68, rush_order=False, jam=False, stop_time=180):
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
        input=inputs,
        output=OUTPUTS,
    )


@pytest.mark.skipif(not FMU_PATH.is_file(), reason="Built Smart Warehouse FMU is unavailable")
def test_default_order_loads_six_packages_then_stops_at_the_truck():
    result = simulate()

    assert int(result["truckLoadCount"][-1]) == 6
    assert int(result["ordersPicked"][-1]) == 1
    assert bool(result["orderComplete"][-1])
    assert result["inventoryLevel"][-1] == pytest.approx(76)
    assert result["agvPosition"][-1] == pytest.approx(8.4)
    assert int(result["truckLoadCount"].max()) == 6


@pytest.mark.skipif(not FMU_PATH.is_file(), reason="Built Smart Warehouse FMU is unavailable")
def test_requested_order_quantity_completes_without_overfilling_the_truck():
    result = simulate(order_quantity=3)

    assert int(result["truckLoadCount"][-1]) == 3
    assert int(result["ordersPicked"][-1]) == 1
    assert bool(result["orderComplete"][-1])
    assert int(result["truckLoadCount"].max()) == 3
    assert result["agvPosition"][-1] == pytest.approx(8.4)


@pytest.mark.skipif(not FMU_PATH.is_file(), reason="Built Smart Warehouse FMU is unavailable")
def test_packages_are_picked_transferred_and_agv_returns_for_the_next_pick():
    result = simulate(order_quantity=3, stop_time=55)

    assert np.any(result["pickupPackageVisible"])
    assert np.any((result["pickupProgress"] > 0.2) & (result["pickupProgress"] < 0.9))
    assert np.any(result["transferPackageVisible"])
    assert np.max(result["transferPackageZ"][result["transferPackageVisible"]]) > 5.2
    assert np.min(result["loaderAngle"]) < -0.2 or np.max(result["loaderAngle"]) > 0.2
    assert result["agvPosition"].max() == pytest.approx(8.4)
    assert np.any(result["agvPosition"] < 8.3)
    assert int(result["truckLoadCount"][-1]) >= 1
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
    normal = simulate(order_quantity=2, speed=0.68, stop_time=60)
    rush = simulate(order_quantity=2, speed=0.68, rush_order=True, stop_time=60)

    normal_deliveries = normal["time"][np.diff(np.r_[0, normal["truckLoadCount"]]) > 0]
    rush_deliveries = rush["time"][np.diff(np.r_[0, rush["truckLoadCount"]]) > 0]
    assert len(normal_deliveries) >= 2
    assert len(rush_deliveries) >= 2
    assert rush_deliveries[0] < normal_deliveries[0] - 2.0
    assert rush_deliveries[1] - rush_deliveries[0] == pytest.approx(
        normal_deliveries[1] - normal_deliveries[0], abs=0.15
    )


@pytest.mark.skipif(not SCENE_PATH.is_file(), reason="Generated Smart Warehouse GLB is unavailable")
def test_scene_binds_pickup_conveyor_loader_and_six_truck_slots():
    content = SCENE_PATH.read_bytes()
    magic, version, total_length = struct.unpack_from("<4sII", content)
    chunk_length, chunk_type = struct.unpack_from("<II", content, 12)
    assert magic == b"glTF" and version == 2 and total_length == len(content)
    assert chunk_type == 0x4E4F534A
    document = json.loads(content[20 : 20 + chunk_length].decode("utf-8").rstrip(" \t\r\n\0"))
    nodes = {node["name"]: node for node in document["nodes"] if "name" in node}

    assert "OutboundTransferConveyor" in nodes
    assert "TruckLoaderArm" in nodes
    assert "OutboundDeliveryTruck_trailer_rear" not in nodes
    assert any(b["variable"] == "pickupProgress" for b in nodes["PickArmEndEffector"]["extras"]["fmiBindings"])
    assert any(b["variable"] == "transferPackageZ" for b in nodes["TruckTransferPackage"]["extras"]["fmiBindings"])
    assert any(b["variable"] == "loaderAngle" for b in nodes["TruckLoaderArm"]["extras"]["fmiBindings"])
    assert any(b["variable"] == "shelfPackage2Visible" for b in nodes["ShelfPackage_2"]["extras"]["fmiBindings"])

    truck = nodes["OutboundDeliveryTruck"]
    cargo_nodes = [document["nodes"][index] for index in truck["children"] if document["nodes"][index]["name"].startswith("TruckCargo_")]
    assert len(cargo_nodes) == 6
    thresholds = {
        binding["visibleWhenGreaterThanOrEqual"]
        for node in cargo_nodes
        for binding in node["extras"]["fmiBindings"]
        if binding["variable"] == "truckLoadCount"
    }
    assert thresholds == {1, 2, 3, 4, 5, 6}
