from __future__ import annotations


def apply_scenario_count(scenarios: list, scenario_count: int | None) -> list:
    if not scenario_count or int(scenario_count) <= 0:
        return list(scenarios)
    return list(scenarios[: int(scenario_count)])


def split_scenarios_into_batches(scenarios: list, batch_size: int | None) -> list[list]:
    if not scenarios:
        return []
    if batch_size is None or int(batch_size) <= 0 or batch_size >= len(scenarios):
        return [list(scenarios)]
    batches: list[list] = []
    for start in range(0, len(scenarios), batch_size):
        batches.append(scenarios[start : start + batch_size])
    return batches
