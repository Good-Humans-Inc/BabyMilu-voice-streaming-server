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
assistant turn, runs a stateless structured extraction over the bounded
transcript, and requires at least one validated morning fact before moving the
same attempt through `saving_personalization` to `completed`. Calls without a
meaningful exchange or valid extraction return to `ready_for_call`. Validated
`morningCallPersonalization` is written to the onboarding record and copied to
`users/{verifiedPhone}/miluCall/dailyCall` when that document already exists.

Runtime requirements:

- Application Default Credentials; no new environment secret is consumed.
- Firestore permissions to list the `miluCall` collection group and get/update
  the two user-scoped Milu Call documents.
- A Firestore collection-group index for `planningCall.grant.hash` if the
  project does not already provide the automatic single-field index.
