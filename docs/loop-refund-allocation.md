# Loop refund allocation (18.0.3.54)

Use with Loop Returns 18.0.1.17.0; upgrade both addons together. Payout
Reconciliation continues to provide exact processor-reference matching unchanged.

Refund credit integrations can supply `_shopify_refund_credit_document_ids()`
and `_shopify_refund_credit_amount()` to prove the portion of a combined credit
belonging to one Shopify refund document. The default remains one document for
the full credit. Loop derives permitted documents and amounts from disjoint
refund entries, rather than relaxing duplicate or total checks.

Reviewed references retain one row per Shopify transaction. Previously reviewed
portions can settle independently; new portions must use their own exact source
identity and fit their assigned credit amount. Historical refund application
checks the exact decrease in the credit residual and full allocation of each
payment. It can leave the unprocessed documented portion open.

Posting dates, customer/company/currency validation, duplicate payout checks,
existing-payment validation, receipt gates and reviewed reference fingerprints
continue to apply. Neither upgrade generates accounting entries by itself.
