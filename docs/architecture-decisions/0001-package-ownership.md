# Package ownership before the first release

Status: accepted

## Decision

TailorCV has six top-level Python boundaries under `src/tailorcv`:

- `bootstrap` composes the API, CLI, worker, and runtime setup;
- `domain` owns framework-free vocabulary, identity, and invariants;
- `application` owns deterministic use cases grouped by product capability;
- `infrastructure` owns databases, artifacts, provider SDKs, and web reads;
- `delivery` adapts application services to HTTP and terminal commands; and
- `shared` holds framework-neutral primitives used by several capabilities.

Domain and shared are the inward boundaries. Application code does not import
delivery or bootstrap. Infrastructure can implement effects requested by the
application, but it does not import delivery or bootstrap. Delivery does not
import ORM models. Bootstrap owns composition.

The SPA keeps route files under `web/src/pages`, puts feature implementation
under `web/src/features`, and keeps generated transport and schema files under
`web/src/api`. A shared frontend component or hook needs more than one
production feature; a single consumer owns its code directly.

## Consequences

Import Linter checks the Python dependency direction. The generated OpenAPI
contract checks the HTTP boundary. Capability-specific code has one
discoverable owner, while shared packages are kept small enough that they do not
become a second application layer.

This decision records current ownership, not the file moves that established
it. [ARCHITECTURE.md](../../ARCHITECTURE.md) explains the runtime boundaries;
[backend development](../contributing/backend-development.md) and [frontend
development](../contributing/frontend-development.md) own placement instructions.
