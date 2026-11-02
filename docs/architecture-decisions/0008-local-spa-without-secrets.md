# A local SPA that cannot see secrets

Status: accepted

## Decision

A same-origin React app behind a loopback-only FastAPI server. TypeScript
contracts are generated from OpenAPI. Both the checked-in document and the
generated declarations are drift gates in CI. The browser receives structured
outcomes, never API keys or raw cache payloads. Durable user data lives outside
the repository under one resolved data root. The default is a visible Documents
folder. Custom SQLite keeps implicit workspaces and artifacts beside the
database. Artifacts are metadata plus replaceable storage URIs.

## The API playground

The in-app API playground reads the live OpenAPI document. It derives its
operation list, parameters, and samples from it rather than maintaining a
parallel catalog. Non-GET operations require acknowledgment. Streaming and
binary bodies execute through the task and file surfaces that own their safety
behavior.

FastAPI's stock Swagger page depends on remote CDN assets. This application's
content-security policy blocks them. So `/api/v1/docs` redirects to the
same-origin playground rather than weakening the policy.
