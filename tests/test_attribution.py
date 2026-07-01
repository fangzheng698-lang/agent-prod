from agent_prod.gates.attribution import AttributionEngine


def test_attribute_counts_added_and_removed_decisions():
    report = AttributionEngine.attribute(
        field="latency_p95_ms",
        baseline_value=100,
        candidate_value=125,
        baseline_decisions=[
            {"decision_id": "kept", "tool_calls": []},
            {"decision_id": "removed", "tool_calls": []},
        ],
        candidate_decisions=[
            {"decision_id": "kept", "tool_calls": []},
            {"decision_id": "added", "tool_calls": []},
        ],
    )

    assert report.decisions_added == 1
    assert report.decisions_removed == 1
    assert {diff.decision_id for diff in report.decision_diffs} == {
        "kept",
        "removed",
        "added",
    }
