from __future__ import annotations

from ahf_finance_agent.agent_card import get_agent_card

REQUIRED_SKILL_IDS = {
    "get_invoice_status",
    "search_invoices_by_vendor",
    "get_payment_clearing_status",
    "get_purchase_order_status",
    "get_purchase_requisition_status",
    "get_vendor_details",
    "search_policy_docs",
}


def test_agent_card_has_all_in_scope_skills():
    card = get_agent_card("https://finance-ai-chatbot.example.com")
    assert {s.id for s in card.skills} == REQUIRED_SKILL_IDS


def test_agent_card_url_is_normalised_with_trailing_slash():
    card = get_agent_card("https://finance-ai-chatbot.example.com/")
    assert card.url == "https://finance-ai-chatbot.example.com/"


def test_agent_card_advertises_streaming_and_push():
    card = get_agent_card("https://x")
    assert card.capabilities.streaming is True
    assert card.capabilities.push_notifications is True
    assert card.model_dump(by_alias=True)["capabilities"]["pushNotifications"] is True


def test_agent_card_is_read_only_in_description():
    card = get_agent_card("https://x")
    assert "read-only" in card.description.lower()
    # No skill should imply a write action.
    for skill in card.skills:
        blob = f"{skill.name} {skill.description}".lower()
        assert not any(w in blob for w in (" approve ", " post ", " release ", " pay "))


def test_served_agent_card_endpoint(client):
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "AHF Finance AI ChatBot"
    assert body["url"] == "https://finance-ai-chatbot.example.com/"
    assert {s["id"] for s in body["skills"]} == REQUIRED_SKILL_IDS


def test_legacy_agent_card_endpoint_still_served(client):
    # scripts/create-destination.sh and older Joule builds probe this path.
    assert client.get("/.well-known/agent.json").status_code == 200
