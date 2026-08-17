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
