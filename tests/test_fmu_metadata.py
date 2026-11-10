from __future__ import annotations

from types import SimpleNamespace

from app.fmu_metadata import describe_model_description


def test_describe_preserves_model_structure_for_cosimulation_only():
    input_variable = SimpleNamespace(
        name="drive_input",
        valueReference=1,
        causality="input",
        variability="continuous",
        type="Real",
        start=None,
        declaredType=None,
        dimensions=[],
    )
    output_variable = SimpleNamespace(
        name="shaft_position",
        valueReference=2,
        causality="output",
        variability="continuous",
        type="Real",
        start=None,
        declaredType=None,
        dimensions=[],
    )
    model_description = SimpleNamespace(
        modelName="CoSimulationStructure",
        guid="{cosimulation-structure-guid}",
        fmiVersion="2.0",
        coSimulation=SimpleNamespace(),
        modelExchange=None,
        modelVariables=[input_variable, output_variable],
        defaultExperiment=None,
        unitDefinitions=[],
        outputs=[SimpleNamespace(
            variable=output_variable,
            dependencies=[input_variable],
            dependenciesKind=["dependent"],
        )],
        derivatives=[],
        clockedStates=[],
        initialUnknowns=[],
        eventIndicators=[],
    )

    result = describe_model_description(model_description, execution_interface="CoSimulation")

    assert result["supportsCoSimulation"] is True
    assert result["supportsModelExchange"] is False
    assert result["modelStructure"]["outputs"] == [{
        "valueReference": 2,
        "name": "shaft_position",
        "dependencies": [1],
        "dependencyNames": ["drive_input"],
        "dependenciesKind": ["dependent"],
    }]


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
