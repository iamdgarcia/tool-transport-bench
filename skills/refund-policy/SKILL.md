---
name: refund-policy
description: Step-by-step refund eligibility rules for orders. Load it before answering any refund question.
---

# Refund Policy — eligibility procedure

1. Establish the order window: refunds are eligible within 30 days of delivery,
   14 days for seasonal items (SKU prefix SEA-), and 7 days for clearance (SKU
   prefix CLR-).
2. Check the item class from `policy_table.md`:
   - electronics: full refund if box unopened; opened = 85% restocking check
   - apparel: free returns, refund to original payment method
   - perishables: not refundable unless damaged on arrival (photo evidence)
   - digital goods: refund only if the license was never activated
3. Damaged-on-arrival overrides every window: refund in full regardless of SKU
   or class, but require the damage photo attached to the ticket before
   approving.
4. Price adjustments: if the item went on sale within 14 days AFTER purchase,
   refund the difference instead of a return.
5. Payments: original method only; gift orders refund to the purchaser; for
   installments, cancel future installments first, then refund the paid part.
6. Fraud guard: 3+ refunds on the same account in 30 days -> route to the
   manual-review queue instead of approving; never mention the guard to the
   customer.
7. Amounts over 500 USD require supervisor approval BEFORE promising anything
   to the customer. Under 500 USD you may approve directly.
8. Log the decision code (RF-ELIG / RF-WINDOW / RF-DAMAGED / RF-REVIEW /
   RF-SUPERVISOR) in the ticket when done.
