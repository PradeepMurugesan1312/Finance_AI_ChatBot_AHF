# Invoice Processing and Payment Terms

> SAMPLE / PLACEHOLDER POLICY — replace with the approved AP policy before pilot.

## Standard payment terms

Default terms are Net 30 from invoice date. Net 45 and Net 60 apply only where
the signed contract specifies them. Prepayment requires CFO approval.

## Early payment discount

If terms offer an early-payment discount (for example 2/10 Net 30), AP takes the
discount whenever the invoice is approved in time. Missing an available discount
over 250 USD is reported monthly.

## When the clock starts

The payment due date is calculated from the later of the invoice date and the
date a valid, matched invoice is received by AP. An invoice blocked for a
three-way match discrepancy does not start the clock until the block is cleared.

## Match tolerances

- Price: the invoice unit price may exceed the PO price by up to 5 percent or
  100 USD per line, whichever is lower.
- Quantity: the invoice quantity may not exceed the received quantity.

Outside tolerance the invoice is blocked for payment and routed to the
requester.

## Payment run schedule

Payment runs execute twice weekly, Tuesday and Friday. An invoice approved and
unblocked before noon the day before is included in the next run.

## Checking whether an invoice has paid

"Posted" means the invoice is booked but not necessarily paid. "Cleared" means a
payment has been applied against it. Ask this chatbot for the payment clearing
status of the accounting document to confirm an actual payment, not just the
invoice status.

## Duplicate invoices

S/4HANA blocks exact duplicate invoice numbers per vendor. Suspected duplicates
with a different number are investigated by AP before payment.

## Disputed invoices

A disputed invoice is blocked for payment with a reason code and the dispute is
tracked to resolution. Suppliers are told to send a corrected invoice rather
than a credit memo where possible.
