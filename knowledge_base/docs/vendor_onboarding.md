# Vendor Onboarding

> SAMPLE / PLACEHOLDER POLICY — replace with the approved vendor onboarding procedure before pilot.

## Steps

1. Requesting department submits a New Vendor Request with the vendor's legal
   name, address, primary contact, and the reason for adding them.
2. Vendor completes the onboarding packet: W-9 or W-8 tax form, banking details
   on bank letterhead, and a signed code-of-conduct acknowledgement.
3. Procurement runs sanctions and debarment screening.
4. AP Master Data validates tax and banking details and creates the business
   partner record in S/4HANA.
5. The business partner is assigned a company code and purchasing organization
   before any PO can be issued to them.

## Standard timeline

Five to seven business days from a complete packet. Incomplete packets are the
most common cause of delay.

## Checking status

The requesting department can ask this chatbot for the vendor's business partner
record to see whether it exists and which company codes it is extended to. A
vendor that returns no business partner record has not been created yet.

## Banking changes

A change to existing vendor banking details requires an out-of-band callback to
a known contact at the vendor to confirm the request, before AP Master Data
applies it. This control exists to prevent payment redirection fraud.

## Rush requests

A rush onboarding can be requested for a contract that is already signed and
time-critical. It still requires the full packet and sanctions screening; only
the internal queue priority changes.
