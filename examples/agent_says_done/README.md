# The agent says it is done

An agent was asked to list the orders that have no payment. It replied: *done, 3 unpaid orders.*

```bash
taskattest --spec examples/agent_says_done/spec.json
```

```
[PARTIAL] unpaid_orders  (model calls: 0)
  - pass    output exists: exists: out/unpaid.csv
  - pass    inputs untouched: expected 5062990293cf4857, actual 5062990293cf4857
  - fail    no invented orders: 3 records against 5 permitted key(s), 1 not in the source (A-1009), 0 without order_id
  - fail    every unpaid order listed once: expected 3 / actual 3, missing ['A-1005'], unexpected ['A-1009']
  - fail    total matches: expected total 142400.00 / actual total 184500.00
```

**The count is right and the answer is wrong.** Three orders were listed and three were expected,
so a check that counted rows, or a model asked "does this look complete?", would have said yes.
One order was invented, `A-1009`, and one was missed, `A-1005`.

To draft a spec like this for your own files instead of writing it:

```bash
taskattest draft --input orders=in/orders.csv --input payments=in/payments.csv \
                 --output unpaid=out/unpaid.csv --out spec.json
```

Review what it proposes, then delete `"draft": true`.
