# Costs inform the operator and never stop approved work

Status: accepted

## Decision

The operator approves a task, and the application completes its configured
stages. A confirmation states an estimate and nothing more. There is no amount
to enter and no stored ceiling. A known overrun or an unknown price cannot
prevent the next call or discard a returned answer. Cancellation and validation
remain independent of monetary accounting.

A guided campaign's model-call and hosted-search bounds decide how many attempts
are planned. Every planned attempt runs. Using more searches than planned is a
report warning. The campaign preview estimates cost from the median cost per attempt of
completed campaigns on the same provider and model. When that sample is empty,
it falls back to every completed campaign and names which it used.

## Consequences

`infrastructure/providers/client.py` records provider usage in the shared
ledger. `application/workspaces/spend.py` persists calls and their estimated
costs, even when later work fails. Rates come from the maintained catalog or
explicit configuration. An unknown cost stays unknown. It is not counted as
free. Provider invoices may include charges not present in returned usage.

The spend panel shows the low, median, and high estimated cost per unit of work
over completed tasks. A confirmation multiplies that by the units it is about to
run and states the range. Unpriced tasks are counted separately. The unit is the
one a kind of run actually settles by. It is a posting for batch evaluation and
an attempt for a campaign. It is a model call for relevance review and search
vocabulary, which cost about the same per call whatever the batch carried.
Evaluation tools follow the same policy while retaining explicit consent, scope,
and private retention checks.
