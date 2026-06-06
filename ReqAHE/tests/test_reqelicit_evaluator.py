from reqahe.envs.dataset import Scenario
from reqahe.envs.reqelicit import ReqElicitSession, _normalize_action_type
from reqahe.llm.client import OpenAICompatibleClient


def test_normalize_action_type_maps_common_aliases() -> None:
    assert _normalize_action_type("ask_question", "ask_question") == "probe"
    assert _normalize_action_type("finish_interview", "finish_interview") == "finish"
    assert _normalize_action_type("CLARIFY", "ask_question") == "clarify"
    assert _normalize_action_type("weird_label", "finish_interview") == "finish"
    assert _normalize_action_type("weird_label", "ask_question") == "probe"


def test_validate_evaluator_accepts_alias_action_type() -> None:
    session = ReqElicitSession(
        Scenario(
            scenario_id="train_0001",
            name="train_0001",
            app_type="test",
            initial_req="initial",
            implicit_requirements=[{"id": "IR1", "Aspect": "Content", "RequirementText": "req"}],
            final_requirements=[],
            raw={},
        ),
        OpenAICompatibleClient(),
        oracle_model="oracle",
        evaluator_model="evaluator",
    )
    result = session._validate_evaluator(
        {
            "action_type": "ask_question",
            "hit": True,
            "hit_requirement_ids": ["IR1"],
            "reasoning": "elicited requirement",
        },
        fallback_action="ask_question",
    )
    assert result["action_type"] == "probe"
    assert result["hit"] is True
    assert result["hit_requirement_ids"] == ["IR1"]


def test_validate_evaluator_ignores_unknown_requirement_ids() -> None:
    session = ReqElicitSession(
        Scenario(
            scenario_id="train_0002",
            name="train_0002",
            app_type="test",
            initial_req="initial",
            implicit_requirements=[{"id": "IR1", "Aspect": "Content", "RequirementText": "req"}],
            final_requirements=[],
            raw={},
        ),
        OpenAICompatibleClient(),
        oracle_model="oracle",
        evaluator_model="evaluator",
    )
    result = session._validate_evaluator(
        {
            "action_type": "probe",
            "hit": True,
            "hit_requirement_ids": ["IR1", "IR999"],
            "reasoning": "partial hit",
        },
        fallback_action="ask_question",
    )
    assert result["hit"] is True
    assert result["hit_requirement_ids"] == ["IR1"]
