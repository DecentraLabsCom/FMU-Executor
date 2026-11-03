from __future__ import annotations

from types import SimpleNamespace

from app.fmu_metadata import describe_model_description


def test_fmi3_clock_attributes_and_event_mode_capability_are_preserved():
    clock = SimpleNamespace(
        name="heartbeat",
        valueReference=2,
        causality="output",
        variability="discrete",
        type="Clock",
        intervalVariability="triggered",
        canBeDeactivated=False,
        supportsFraction=False,
        intervalDecimal=None,
        shiftDecimal=None,
        priority=None,
        resolution=None,
        intervalCounter=None,
        shiftCounter=None,
        start=None,
        initial="calculated",
        clocks=[],
        dimensions=[],
        declaredType=None,
    )
    model_description = SimpleNamespace(
        modelName="BinaryClockTest",
        guid="{bct-test-guid-0001}",
        fmiVersion="3.0",
        coSimulation=SimpleNamespace(
            hasEventMode=True,
            canGetAndSetFMUstate=False,
            canSerializeFMUstate=False,
            canHandleVariableCommunicationStepSize=True,
            providesDirectionalDerivative=False,
            providesAdjointDerivatives=False,
            fixedInternalStepSize=None,
        ),
        modelExchange=None,
        modelVariables=[clock],
        defaultExperiment=None,
        unitDefinitions=[],
    )

    result = describe_model_description(
        model_description,
        execution_interface="CoSimulation",
    )

    assert result["modelVariables"][0]["intervalVariability"] == "triggered"
    assert result["modelVariables"][0]["canBeDeactivated"] is False
    assert result["modelVariables"][0]["supportsFraction"] is False
    assert result["capabilities"]["hasEventMode"] is True
