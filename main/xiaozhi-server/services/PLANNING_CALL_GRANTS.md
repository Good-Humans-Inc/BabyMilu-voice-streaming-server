# Native onboarding call grants

The authenticated Milu Call API issues a cryptographically random, one-time
grant. It returns the raw grant to the app once and stores only its SHA-256 hex
digest in the existing document:

`users/{verifiedPhone}/miluCall/onboarding`

```text
planningCall.status = "call_in_progress"
planningCall.characterId = <server-validated character>
planningCall.attemptId = <server-created attempt>
planningCall.grant = {
  hash: <sha256 hex>,
  expiresAt: <Firestore timestamp>,
  scenario: "daily_call_onboarding",
  characterId: <same character>,
  attemptId: <same attempt>
}
```

The app sends only `connectionType` and `connectionGrant` in its first
WebSocket hello. The voice server performs a collection-group lookup by hash
and atomically writes `planningCall.grant.consumedAt`. Expired, ambiguous,
replayed, mismatched, or client-identified hellos close before profile,
provider, session, or audio initialization.

At a successful conversational close, the server requires both a user and an
assistant turn and runs a stateless structured extraction over the bounded
transcript before atomically moving the same attempt from `call_in_progress`
to `completed`. Personalization is optional: declining, being unsure, or a
transient extractor failure stores a valid empty payload and does not repeat
the call. Calls without a meaningful exchange return to `ready_for_call`. Validated
`morningCallPersonalization` is written to the onboarding record and copied to
`users/{verifiedPhone}/miluCall/dailyCall` when that document already exists.
Every lifecycle write is a Firestore compare-and-set bound to the consumed
grant hash, attempt, character, and expected current status. A delayed retry or
finalizer therefore cannot downgrade `completed` or a newer reissued attempt.
`grant.consumedAt` begins a bounded voice-process lease. Both services use
`MILU_CALL_CONSUMED_LEASE_SECONDS` (default `900`, clamped to `60..3600`): the
backend may recover an abandoned call after the lease, while voice completion
must occur inside it and fails its transaction after recovery resets the state.

Runtime requirements:

- Application Default Credentials; no new environment secret is consumed.
- Optional `MILU_CALL_CONSUMED_LEASE_SECONDS`, shared with the Milu Call API.
- Firestore permissions to list the `miluCall` collection group and get/update
  the two user-scoped Milu Call documents.
- A Firestore collection-group index for `planningCall.grant.hash` if the
  project does not already provide the automatic single-field index.
