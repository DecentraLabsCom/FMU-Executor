from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.fmi_ls_dae import parse_fmi_ls_dae_manifest


NS = "http://fmi-standard.org/fmi-ls-manifest"


def _model_description():
    state = SimpleNamespace(name="x", valueReference=2, type="Float64", causality="local", variability="continuous", initial="exact", shape=None, dimensions=[])
    derivative = SimpleNamespace(name="der_x", valueReference=3, type="Float64", causality="local", variability="continuous", initial="calculated", shape=None, dimensions=[], derivative=state)
    variables = [
        SimpleNamespace(name="time", valueReference=0, type="Float64", causality="independent", variability="continuous", shape=None, dimensions=[]),
        SimpleNamespace(name="enableDAE", valueReference=1, type="Boolean", causality="structuralParameter", variability="tunable", shape=None, dimensions=[]),
        state,
        derivative,
        SimpleNamespace(name="z", valueReference=4, type="Float64", causality="local", variability="continuous", shape=None, dimensions=[]),
        SimpleNamespace(name="u", valueReference=5, type="Float64", causality="input", variability="continuous", shape=None, dimensions=[]),
        SimpleNamespace(name="y", valueReference=6, type="Float64", causality="output", variability="continuous", shape=None, dimensions=[]),
        SimpleNamespace(name="res", valueReference=7, type="Float64", causality="local", variability="continuous", shape=None, dimensions=[]),
        SimpleNamespace(name="z_event", valueReference=8, type="Float64", causality="local", variability="continuous", shape=None, dimensions=[]),
    ]
    return SimpleNamespace(
        fmiVersion="3.0",
        modelExchange=object(),
        modelVariables=variables,
    )


def _manifest(model_structure: str | None = None) -> bytes:
    structure = model_structure or """
      <Output valueReference="6" dependencies="2 4 5" dependenciesKind="dependent dependent dependent"/>
      <ContinuousStateDerivative valueReference="3" dependencies="2 4 5" dependenciesKind="dependent dependent dependent"/>
      <InitialUnknown valueReference="3" dependencies="2 4 5" dependenciesKind="dependent dependent dependent"/>
      <InitialUnknown valueReference="4" dependencies="2 4 5" dependenciesKind="dependent dependent dependent"/>
      <EventIndicator valueReference="8" dependencies="2" dependenciesKind="dependent"/>
      <Residual valueReference="7" dependencies="2 4 5" dependenciesKind="dependent dependent dependent"/>
    """
    return f"""<fmiDAEManifest xmlns:fmi-ls="{NS}"
        fmi-ls:fmi-ls-name="org.fmi-standard.fmi-ls-dae"
        fmi-ls:fmi-ls-version="1.0.0-alpha.1"
        fmi-ls:fmi-ls-description="Layered standard for DAE support in FMI.">
      <EnableDAEParameter valueReference="1"/>
      <AlgebraicVariables><AlgebraicVariable valueReference="4"/></AlgebraicVariables>
      <ModelStructure>{structure}</ModelStructure>
    </fmiDAEManifest>""".encode()


def test_dae_manifest_parses_complete_core_model_structure_and_dependencies():
    definition = parse_fmi_ls_dae_manifest(_manifest(), _model_description())

    assert definition.enable_dae_variable_name == "enableDAE"
    assert definition.algebraic_names == ("z",)
    assert definition.residual_names == ("res",)
    assert definition.derivative_value_references == (3,)
    assert definition.initial_unknown_value_references == (3, 4)
    assert definition.event_indicator_value_references == (8,)
    residual = next(item for item in definition.model_structure if item.kind == "Residual")
    assert residual.dependencies == (2, 4, 5)
    assert residual.dependencies_kind == ("dependent", "dependent", "dependent")


def test_dae_manifest_rejects_unknown_model_structure_elements():
    structure = '<Invariant valueReference="7"/>'
    with pytest.raises(ValueError, match="unsupported ModelStructure element"):
        parse_fmi_ls_dae_manifest(_manifest(structure), _model_description())

    namespaced_structure = '<x:Output xmlns:x="urn:invalid" valueReference="6"/>'
    with pytest.raises(ValueError, match="unsupported ModelStructure element"):
        parse_fmi_ls_dae_manifest(_manifest(namespaced_structure), _model_description())


def test_dae_manifest_rejects_invalid_dependency_references_and_kinds():
    bad_reference = '<Residual valueReference="7" dependencies="999"/>'
    with pytest.raises(ValueError, match="unknown dependency valueReference"):
        parse_fmi_ls_dae_manifest(_manifest(bad_reference), _model_description())

    bad_kind_count = '<Residual valueReference="7" dependencies="2 4" dependenciesKind="dependent"/>'
    with pytest.raises(ValueError, match="dependenciesKind count"):
        parse_fmi_ls_dae_manifest(_manifest(bad_kind_count), _model_description())

    output_dependency = '<Output valueReference="6" dependencies="6"/>'
    with pytest.raises(ValueError, match="dependencies must reference known"):
        parse_fmi_ls_dae_manifest(_manifest(output_dependency), _model_description())


def test_dae_manifest_rejects_invalid_model_structure_order():
    structure = '<Residual valueReference="7"/><Output valueReference="6"/>'
    with pytest.raises(ValueError, match="invalid ModelStructure order"):
        parse_fmi_ls_dae_manifest(_manifest(structure), _model_description())


def test_dae_manifest_validates_output_and_initial_unknown_roles():
    invalid_output = '<Output valueReference="4"/>'
    with pytest.raises(ValueError, match="Output must reference an output"):
        parse_fmi_ls_dae_manifest(_manifest(invalid_output), _model_description())

    event_indicator_as_initial_unknown = '<InitialUnknown valueReference="8"/>'
    with pytest.raises(ValueError, match="does not reference an initialization unknown"):
        parse_fmi_ls_dae_manifest(_manifest(event_indicator_as_initial_unknown), _model_description())


def test_dae_manifest_restricts_initial_unknown_dependency_kinds():
    structure = '<InitialUnknown valueReference="3" dependencies="2" dependenciesKind="tunable"/>'
    with pytest.raises(ValueError, match="unsupported dependenciesKind"):
        parse_fmi_ls_dae_manifest(_manifest(structure), _model_description())


def test_dae_manifest_requires_layered_standard_description():
    xml = _manifest().replace(
        b' fmi-ls:fmi-ls-description="Layered standard for DAE support in FMI."',
        b"",
    )
    with pytest.raises(ValueError, match="description is missing or unsupported"):
        parse_fmi_ls_dae_manifest(xml, _model_description())


def test_dae_integrator_rejects_event_indicators_it_cannot_integrate():
    definition = parse_fmi_ls_dae_manifest(_manifest(), _model_description())
    with pytest.raises(ValueError, match="event indicators is not supported"):
        from app.fmi_ls_dae import Fmi3DaeIntegrator

        Fmi3DaeIntegrator(
            object(),
            definition,
            model_description=_model_description(),
            start_time=0,
        )
