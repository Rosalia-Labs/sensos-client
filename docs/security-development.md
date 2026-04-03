# Developer Security Notes

This document is for developers and maintainers. It is not an end-user deployment checklist.

The goal is to describe the actual security model of the current client/server design, especially where the design intentionally accepts risk or where the risk cannot be eliminated with the current architecture.

If your use case requires a different security model or stronger security properties than the baseline described here, reach out to Rosalia Labs and let us know what you need.

Deployment context matters. Devices in publicly accessible locations face a meaningfully different risk profile than devices on private land or in other controlled-access areas, and that context is often relevant in institutional reviews.

## Core Constraint

Without hardware-backed secret storage, the client cannot avoid holding a live server credential.

In the current design:

- the client must authenticate to the server API
- the client stores the client API password on disk at `/sensos/keys/api_password`
- the client also stores WireGuard material on disk

That means:

- if an attacker gains local access to the device, they can likely recover the credential material needed to act as a client
- if an attacker can image or copy the device storage, they can likely recover the same material offline

This is not a bug in one script. It is a consequence of the trust model.

## What The Client Secret Buys An Attacker

If an attacker can read the client disk or obtain local privileged access, they can likely:

1. recover the client API password
2. recover enough network configuration to understand how the client reaches the server
3. create or register a WireGuard peer
4. reach the server API over the WireGuard network

In other words, possession of the client image is close to possession of the client identity.

## Current Boundary That Still Helps

The current deployment does have an important containment property:

- the server API is only reachable over WireGuard
- the WireGuard network is not bridged onto the host network
- there is a dedicated container for WireGuard orchestration
- there is a separate WireGuard-facing reverse proxy container
- API access dead-ends into Docker containers on the server host
- the reverse proxy forwards to a separate API container
- the API container talks to a separate database container
- there is not intended to be a native direct path from the API container to the server host

This means the exposed server-side path is segmented and relatively narrow:

- WireGuard orchestration container
- WireGuard-facing reverse proxy
- API container
- database container

That is a meaningfully smaller direct attack surface than exposing the full application stack or the host itself to the client network.

So the expected attack chain is not:

- steal client secret
- directly gain host-level access to the server

It is instead:

- steal client secret
- become a WireGuard peer
- reach the reverse proxy and then the API inside the containerized server environment
- attempt API abuse or container compromise
- then attempt container escape or lateral movement to reach the host

That is meaningfully better than exposing the API directly on the host network, but it does not remove the client-secret problem.

## Expected Attack Path

With the current design, the realistic attack path is:

1. gain local login on the device or physical access to the storage media
2. recover the client API password and other client config
3. create a WireGuard peer or otherwise impersonate the client
4. send API commands to the SensOS server over WireGuard
5. gain access to one or more Docker containers through API misuse or container-level compromise
6. attempt to break out of the container boundary to reach the server host

This is the chain developers should keep in mind when evaluating changes.

It is also worth noting what this is not:

- it is not a direct client-to-host management path
- it is not a direct client-to-database path
- it is not a flat network exposing the entire server stack

## What This Means For Design Decisions

### Do not assume the client is a strong secret boundary

A deployed client should be treated as recoverable by a determined local attacker.

Implication:

- anything placed on the client should be assumed extractable if the device or disk is lost

### Treat the API as reachable by a stolen client identity

Even though the API is behind WireGuard, the client credential model means a stolen client may be enough to re-enter that network.

Implication:

- the API should not trust a client merely because it arrived over WireGuard
- API actions should be minimized, constrained, and auditable

### Treat container isolation as a secondary boundary, not the primary one

Containerization helps. It should not be the only thing standing between API misuse and host compromise.

Implication:

- minimize privileges in the server containers
- avoid mounting sensitive host paths into containers unless required
- keep container-to-host trust narrow and explicit

## Security Priorities For Future Work

Given the current architecture, the highest-value improvements are:

### 1. Reduce what a stolen client can do

Examples:

- scope client API credentials more tightly
- separate enrollment credentials from steady-state credentials
- rotate or revoke client credentials cleanly
- avoid giving every client a credential that can perform broad administrative operations

### 2. Reduce server-side blast radius

Examples:

- tighten API authorization
- make sensitive API actions require narrower roles
- improve per-client auditability
- isolate Docker services from one another more aggressively

### 3. Improve revocation and incident response

Examples:

- define how to revoke a lost device
- define how to invalidate a copied client identity
- define how to rotate API credentials and WireGuard trust after compromise

### 4. Add hardware-backed secret protection if the threat model requires it

If the system eventually needs strong resistance to disk theft or offline cloning, the current model is not enough.

Examples:

- TPM-backed secrets
- secure element integration
- attested device identity
- remote enrollment flows that avoid long-lived reusable shared credentials

Note:

- hardware-backed secret handling is possible, but it is not part of the current baseline design
- if your use case requires stronger security properties, reach out and let us know what you need

## Non-Goals Of The Current Design

With the current client architecture, the system does not provide:

- protection against an attacker who can read the client disk image
- protection against an attacker with local privileged access to the client
- protection against client identity cloning

Developers should avoid accidentally documenting or implying stronger guarantees than the implementation actually provides.

## Practical Guidance For Developers

When making changes:

- assume the client can be copied
- assume secrets on the client can be extracted
- ask what a stolen client can do next
- ask whether the API action being added increases server-side blast radius
- ask whether the container boundary is being treated as the only remaining line of defense

If a new feature requires a powerful server-side credential on the client, that should be called out explicitly in design review.
