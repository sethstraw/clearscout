# One set of services, several ways in

Status: accepted

## Decision

Typer, FastAPI, and the worker are all just ways in to the same application
services. The web server never shells out to the CLI. Domain vocabulary carries
no framework dependency. Service methods own their transaction boundaries, and
provider adapters sit behind explicit interfaces. Local artifact storage stays
one concrete thing until there is actually a second backend.

## Consequences

Do it the other way and you get a second, web-only copy of the same rules, and
the two of them disagree inside a month.
