from prayog.commands.access import describe_state


def test_describe_rejected_access_request():
    result = describe_state({
        "final_status": "REJECTED",
    })

    assert result == "REJECTED. See your request for who rejected it."