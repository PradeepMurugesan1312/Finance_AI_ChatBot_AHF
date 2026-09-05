# Knowledge base source documents

Drop the finance / AP / procurement SOPs and policy documents here as `.md` or
`.txt` files, then rebuild the index:

```bash
python -m ahf_finance_agent.kb_ingest
```

The server also rebuilds the index on startup when `knowledge_base/index.json`
is missing or `KB_REBUILD_ON_START=true`, so a fresh deploy ships a working KB.

## Rules of thumb

- **One topic per file** where practical. Headings (`#`, `##`, …) become the
  "section" shown in citations, so use them.
- **Curate before you ingest.** Outdated or conflicting policy text is retrieved
  and repeated verbatim by the bot. Removing stale content is a
  finance-stakeholder task and the real critical path — not the ingest script.
- **No PII or banking data** in these files. The bot cites and quotes them.
- Keep the language plain and answer-shaped ("The approval threshold for a
  single PO is …"), not narrative.

## What's here now

`po_approval_thresholds.md`, `travel_expense_policy.md`, `vendor_onboarding.md`,
and `invoice_payment_terms.md` are **SAMPLE / PLACEHOLDER** content written to
exercise the retrieval pipeline end-to-end. Replace them with the real approved
documents (or the content pulled from the SAP HANA policy store) before any
pilot.
