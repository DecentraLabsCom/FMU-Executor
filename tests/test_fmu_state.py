from app import fmu_state


def test_checkpoint_capture_boundary_is_only_inside_the_last_requested_step():
    assert fmu_state.is_checkpoint_before_final_step(0.01, 0.02, 0.01)
    assert fmu_state.is_checkpoint_before_final_step(0.04, 0.05, 0.01)
    assert fmu_state.is_checkpoint_before_final_step(0.0, 0.01, 0.01)
    assert not fmu_state.is_checkpoint_before_final_step(0.01, 0.05, 0.01)
    assert not fmu_state.is_checkpoint_before_final_step(0.05, 0.05, 0.01)
    assert not fmu_state.is_checkpoint_before_final_step(float("nan"), 0.05, 0.01)
