"""Full A2A `message/send` round trip through the real Starlette app.

Step 1 has no external dependencies to mock — this exercises the JSON-RPC
endpoint, the executor's task lifecycle, and the exact response shape Joule's
dialog function reads.
"""

from __future__ import annotations

# a2a-sdk serialises TaskState.input_required as "input-required".
INPUT_REQUIRED = "input-required"
TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}


def _result(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    return body["result"]


def test_message_send_returns_task_with_answer_in_status_message(client, send_message):
    result = _result(send_message(client, "What is the status of invoice 5105601234?"))

    assert result["kind"] == "task"
    assert result["id"]
    assert result["contextId"]

    # Joule reads result.body.status.message.parts[0].text — must be populated
    # with the generated answer (fake LLM in tests).
    text = result["status"]["message"]["parts"][0]["text"]
    assert text == "Fake model reply."


def test_generated_answer_is_scrubbed_before_returning(client, send_message, fake_llm):
    fake_llm.reply = "Wire it to IBAN DE89 3704 0044 0532 0130 00 today."
    result = _result(send_message(client, "where do I send payment?"))
    text = result["status"]["message"]["parts"][0]["text"]
    assert "DE89" not in text
    assert "[redacted]" in text


def test_task_never_reaches_terminal_state(client, send_message):
    """a2a-sdk rejects message/send on a terminal task; that broke every Joule
    follow-up turn for the sibling agent. The task must stay resumable."""
    result = _result(send_message(client, "hello"))
    assert result["status"]["state"] == INPUT_REQUIRED
    assert result["status"]["state"] not in TERMINAL_STATES


def test_follow_up_turn_on_same_task_is_accepted(client, send_message):
    first = _result(send_message(client, "hello", req_id="1"))
    second = send_message(
        client,
        "and what about payment?",
        task_id=first["id"],
        context_id=first["contextId"],
        req_id="2",
    )
    result = _result(second)
    assert result["contextId"] == first["contextId"]
    assert result["status"]["state"] == INPUT_REQUIRED


def test_answer_also_attached_as_artifact(client, send_message):
    """Joule ignores artifacts, but the direct-API demo page and other A2A
    clients read them."""
    result = _result(send_message(client, "hello"))
    artifact_text = result["artifacts"][0]["parts"][0]["text"]
    status_text = result["status"]["message"]["parts"][0]["text"]
    assert artifact_text == status_text


def test_unknown_jsonrpc_method_is_a_clean_error(client):
    resp = client.post("/", json={"jsonrpc": "2.0", "id": "x", "method": "does/notExist"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == -32601  # method not found
