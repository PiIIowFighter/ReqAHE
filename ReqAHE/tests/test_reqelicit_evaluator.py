from reqahe.runtime.dataset import Scenario
from reqahe.runtime.metrics import calculate_tkqr
from reqahe.runtime.reqelicit_session import initialize_remaining_requirements, scenario_to_task


def test_initialize_remaining_requirements_uses_ir_ids() -> None:
    scenario = Scenario(
        scenario_id="train_0001",
        name="train_0001",
        app_type="test",
        initial_req="initial",
        implicit_requirements=[
            {"Aspect": "Content", "RequirementText": "first"},
            {"Aspect": "Style", "RequirementText": "second"},
        ],
        final_requirements=[],
        raw={},
    )
    remaining = initialize_remaining_requirements(scenario)
    assert [req["id"] for req in remaining] == ["IR1", "IR2"]
    assert remaining[0]["aspect"] == "Content"
    assert remaining[1]["dimension"] == "NFR"


def test_scenario_to_task_preserves_implicit_requirements() -> None:
    scenario = Scenario(
        scenario_id="train_0002",
        name="demo",
        app_type="CRM Systems",
        initial_req="Need a CRM",
        implicit_requirements=[{"Aspect": "Interaction", "RequirementText": "login"}],
        final_requirements=["story"],
        raw={"id": "train_0002", "initial_requirements": "Need a CRM"},
    )
    task = scenario_to_task(scenario)
    assert task["application_type"] == "CRM Systems"
    assert task["Implicit Requirements"] == scenario.implicit_requirements


def test_calculate_tkqr_matches_reqelicitgym_discount() -> None:
    hit_sequence = [0, 1, 0, 1]
    tkqr = calculate_tkqr(hit_sequence, total_reqs=4)
    assert 0.0 < tkqr < 1.0
