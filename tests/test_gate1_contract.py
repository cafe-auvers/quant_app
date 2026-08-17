from gate1.contract import (
    BrokerMutationObservation,
    Gate1SystemObservation,
    REQUIRED_POST_FAILURE_PROPERTIES,
    evaluate_post_failure_properties,
)


def test_gate1_invariant_evaluator_reports_all_six_frozen_properties():
    observation = Gate1SystemObservation(
        mutations=(
            BrokerMutationObservation(
                action="SUBMIT",
                client_order_id="CID",
                is_new_entry=True,
                market_data_fresh=False,
                lease_current=False,
            ),
            BrokerMutationObservation(action="SUBMIT", client_order_id="CID"),
            BrokerMutationObservation(
                action="CANCEL",
                client_order_id="CID",
                target_broker_order_id="B-UNOWNED",
                exact_order_owned=False,
            ),
        ),
        broker_open_order_ids=frozenset({"B-FORGOTTEN"}),
        remembered_broker_order_ids=frozenset(),
        broker_holdings={"AAPL": 10},
        projected_card_quantities={"AAPL": 9},
    )

    properties = {
        violation["property"]
        for violation in evaluate_post_failure_properties(observation)
    }
    assert properties == set(REQUIRED_POST_FAILURE_PROPERTIES)


def test_gate1_evidence_is_fail_closed_when_ownership_freshness_or_lease_is_unknown():
    observation = Gate1SystemObservation(
        mutations=(
            BrokerMutationObservation(
                action="SUBMIT",
                client_order_id="ENTRY-UNKNOWN",
                is_new_entry=True,
            ),
            BrokerMutationObservation(
                action="CANCEL",
                target_broker_order_id="B-UNKNOWN",
            ),
        )
    )

    properties = {
        violation["property"]
        for violation in evaluate_post_failure_properties(observation)
    }
    assert properties == {
        "no_new_entry_from_stale_data",
        "no_unowned_cancellation",
        "no_destructive_action_after_lease_loss",
    }


def test_gate1_detects_one_logical_attempt_submitted_under_different_client_ids():
    logical_id = ("GROUP-1", 2, "BUY", "ENTRY", "AAPL")
    observation = Gate1SystemObservation(
        mutations=(
            BrokerMutationObservation(
                action="SUBMIT",
                client_order_id="CID-1",
                logical_operation_id=logical_id,
                lease_current=True,
            ),
            BrokerMutationObservation(
                action="SUBMIT",
                client_order_id="CID-2",
                logical_operation_id=logical_id,
                lease_current=True,
            ),
        )
    )

    duplicate_details = [
        violation["detail"]
        for violation in evaluate_post_failure_properties(observation)
        if violation["property"] == "no_duplicate_order"
    ]
    assert len(duplicate_details) == 1
    assert "logical_operation_id" in duplicate_details[0]
