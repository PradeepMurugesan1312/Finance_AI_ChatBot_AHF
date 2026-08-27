# Joule Capability — Joule-Ai-Chatbot-Finance (AHF Finance ChatBot)

Technical name: `joule.ext/joule_ai_chatbot_finance` (renamed from
`finance_ai_chatbot_a2a`, which collided with an existing capability).

Connects SAP Joule to the deployed A2A agent as a "bring your own agent" (BYOA)
capability. **This is build step 8** — the files here are ready, but deploying
them needs the Cloud Foundry app live (step 7), the BTP destination in place,
and (for production) an IAS App2App trust. Nothing in this folder is deployed
yet.

## File structure

```
joule-capability/
├── da.sapdas.yaml            # DA deployment descriptor (schema 1.4.0)
├── capability.sapdas.yaml    # capability metadata + system_aliases (schema 3.28.0)
├── capability_context.yaml   # multi-turn context variables (contextId, taskId)
├── functions/call_agent.yaml # the agent-request dialog function
└── scenarios/invoke_agent.yaml  # user intent → function, with context threading
```

Every piece above is required. Missing any one produces a generic
`Dialog function execution failed` with no detail and no network trace reaching
the agent.

## Prerequisites

- **Joule DTA schema 3.28.0+** — BYOA `agent-request` needs it. If
  `joule deploy` fails with a schema-version error, the tenant needs a Joule
  service update (BTP admin).
- Agent deployed to Cloud Foundry (`cf push`, step 7) and reachable at
  `/.well-known/agent-card.json`.
- BTP destination **`AHF_FINANCE_AGENT_DEV`** (already created in the POC
  subaccount) pointing at the CF route, with:
  - `Type: HTTP`, `ProxyType: Internet`
  - `Authentication: NoAuthentication` — **dev/testing only**; production needs
    IAS App2App (OAuth2/SAMLAssertion) so S/4HANA roles still gate access.
  - Additional Properties (not on the default "New Destination" form — add
    manually): `HTML5.DynamicDestination = true`, `WebIDEEnabled = true`
  - `scripts/create-destination.sh` sets all of this.

## Two destinations, don't confuse them

- **`S43`** — agent → S/4HANA (OData). Outbound from the agent.
- **`AHF_FINANCE_AGENT_DEV`** — Joule → agent. Referenced by
  `capability.sapdas.yaml`'s `system_aliases.FinanceChatBotAgent`.

## Deploy

```bash
# 1. (once) create/refresh the Joule → agent destination
bash ../scripts/create-destination.sh \
  --agent-name finance-ai-chatbot \
  --destination-name AHF_FINANCE_AGENT_DEV \
  --landscape us10-001

# 2. deploy the capability
npm install -g @sap/joule-studio-cli
joule login
joule deploy ./da.sapdas.yaml --compile -n "joule_ai_chatbot_finance"
```

Then in Joule: *"What's the status of invoice 5105601234 for fiscal year 2026?"*

## If it fails with an opaque error

Verify, in order:
1. Agent card reachable at the destination URL + `/.well-known/agent-card.json`.
2. `AHF_FINANCE_AGENT_DEV` URL exactly matches the CF app route.
3. `HTML5.DynamicDestination` and `WebIDEEnabled` properties present.
4. Scenario `description` matches how the question is phrased.
5. `cf logs finance-ai-chatbot --recent` — did a request even arrive?

If all of that checks out and Joule still fails every request identically with
`JCore-4004 / PROCESSING_FAILED` (zero detail, no request reaching the agent),
that is a known SAP platform-side condition — collect `log_id` / `correlation_id`
and open an SAP support ticket. The sibling agent hit exactly this. Do not
keep re-verifying config that is already confirmed correct.
