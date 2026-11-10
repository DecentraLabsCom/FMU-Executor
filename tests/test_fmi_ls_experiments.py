from io import BytesIO
import xml.etree.ElementTree as ET
import zipfile

import pytest

from app.fmi_ls_experiments import _parse_experiment


def _parse(xml):
    with zipfile.ZipFile(BytesIO(), "w") as archive:
        return _parse_experiment(
            ET.fromstring(xml),
            experiment_namespace="",
            archive=archive,
            experiment_archive_path="extra/org.fmi-standard.fmi-ls-ref/experiments/demo.exp",
            resource_name_counts={},
            parse_parameter_set=lambda _data: None,
            remaining_parameter_bytes=0,
        )


def test_positive_ma_hs_experiment_tolerance_is_executable():
    case = _parse('<TransientExperiment name="Tight response" tolerance="1e-7"/>')

    assert case["status"] == "ready"
    assert case["options"] == {"tolerance": 1e-7}
    assert case["reason"] == ""


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "not-a-number"])
def test_invalid_ma_hs_experiment_tolerance_is_rejected(value):
    case = _parse(f'<TransientExperiment name="Invalid" tolerance="{value}"/>')

    assert case["status"] == "invalid"
    assert case["reason"] in {"invalid_experiment_tolerance", "invalid_experiment_time_option"}
