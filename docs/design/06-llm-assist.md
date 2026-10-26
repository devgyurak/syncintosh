# 06 — LLM-assisted resolution

Status: **Accepted**, with the gaps listed under *Known limits*.

`syncintosh resolve --llm` asks an OpenAI-compatible model to suggest a choice for each
**content** conflict a stopped sync left pending. The model is never trusted: each suggestion is
masked before it leaves, restored and checked after it returns, and offered to the person, who
accepts or declines it. The daemon never calls a model; it leaves conflicts pending as before.

## Threat model

- **The model is an untrusted party.** It may answer with anything, including a value it invented,
  a link, or text that tries to hide what it is doing. Every answer is checked against a rule in
  code, and nothing it writes is followed as an instruction.
- **The endpoint is untrusted until named.** There is no default. `llm.base_url` must be set, and
  an endpoint that is not loopback or a private network is confirmed before the first call, because
  the conflicting values then leave the machine.
- **The conflict is the secret.** A conflict's sides carry settings text, which may hold tokens,
  internal hostnames, paths that reveal the person, and their username. Those never reach the
  model.

## What leaves the machine

For one content conflict, the model receives four texts: the path, and the base, local and remote
values of the conflicted key or whole file. Every value is masked first (`system/masking.py`):

- every secret pattern the scanner knows, both its structured and its shell table, so a credential
  the scanner would refuse is masked here too;
- the home directory path and the username, in every occurrence;
- internal hostnames and IP addresses, by the built-in rule (`.local`, `.internal`, `.lan`,
  `.corp`, `home.arpa`, `localhost`, loopback, link-local and private IPs) and by the domains the
  config lists in `llm.internal_domains`.

Each masked value becomes a token, `<<snt:NONCE:N>>`. The nonce is drawn per request, so a token
from one request is meaningless in another. The request carries a per-request index, never a
fingerprint: a fingerprint is unsalted and would let a model test a guessed value.

The mapping from token to value stays in the process. It is never sent and never logged; a
`MaskedText` hides it from `repr`.

## What comes back

The answer must be one JSON object with exactly `choice`, `value` and `rationale`, and no key
repeated (`core/advice.py`, `parse_advice`):

- `choice` is `local`, `remote` or `proposal`.
- `value` is a string for a proposal and `null` for a side.
- Anything else, including nesting deep enough to fail the parser, is refused.
- `rationale` has terminal escapes, C0 and C1 control characters, and bidirectional overrides
  removed before anything displays it.

A proposed value is **restored only when it names each token of the masked conflict exactly once
and no other token** (`restore`). A missing token, a duplicated one, an invented one, or a token
from another request refuses the whole proposal. A model therefore cannot slip a value past the
mask, and a proposal that drops a secret from a side is refused rather than applied.

Then the restored value is checked before the person sees it (`proposal_problem`): a key value
must parse as a fragment of its format, and a whole file must parse and pass its preset's syntax
check. A proposal that fails is shown with its reason and declined; the person chooses by hand.

The merge and the publish check the result again (`_checked`, the render round-trip), so a
proposal never reaches a live file unchecked. The pre-check only keeps a bad proposal from being
offered; it is not the gate.

## Who decides

- **Record-level conflicts are always the person's.** An add/add, a delete/modify, a path collision,
  and a modify/modify of a record are never sent to the model.
- **A content conflict is applied without asking only when all three hold:** its preset is listed
  in `llm.auto_apply` (for example `["vscode"]`); the answer is a usable choice that passes the
  check; and it introduces **no new URL**.
- **Every other suggestion is shown,** and the person accepts or declines it. A declined or refused
  suggestion falls back to the person's own choice for that conflict.
- **The final approval always runs,** unless `--yes` is given. It shows every pick the run would
  publish, including the ones the model proposed.

## URLs

A proposal can introduce a link. A URL is *new* when none of the base, local or remote values
already held it. Every new URL is:

- shown with its domain, and labelled `UNTRUSTED <domain>`;
- **never trusted**, even when its text matches a URL a preset ships. Trust comes only from a
  shipped preset's exact download URL (`download_trust`, `ShippedPresets`), and a proposal is not a
  shipped preset.
- a reason the proposal is always asked, even for an auto-applied preset.

## Backup

Before an apply writes over a live file, the sync records a restore point: the commit this cycle
worked from, the collect commit if the cycle wrote one, or the HEAD it kept otherwise. `_apply`
takes that restore point as a required keyword, so no write can be requested without naming it.
The report and the CLI show it.

**The restore point is not yet a guarantee that every overwritten byte is recoverable.** An
`--adopt remote` overwrite, or a file an earlier cycle published but never applied, can replace
bytes that only an older commit holds. Closing that needs a per-file restore point or a stored
pre-image. It is open and tracked as a known limit below.

## The endpoint

- `llm.base_url` has no default. It must be an `http` or `https` URL with a host.
- Plain `http` to a **public** host is refused at config load. The key and the conflict would
  travel in clear.
- A public host (anything not loopback, link-local, private or internal by the rule above) is
  confirmed before the first call. The prompt names the endpoint and says the values will leave
  the network. Declining exits with nothing sent.
- Redirects are not followed. A redirect from the endpoint the person confirmed could send the
  request body to a host they never confirmed.
- The API key comes from `SYNCINTOSH_LLM_API_KEY`, then the keyring. It is never written to
  `config.json`. A home-server model needs no key.

## Configuration

```json
"llm": {
  "base_url": "http://nas.local:11434/v1",
  "model": "a-local-model",
  "internal_domains": ["corp.example"],
  "auto_apply": ["vscode"]
}
```

Every key is optional except that `base_url` and `model` are both needed to run `--llm`. An
unknown key is refused, so a typo cannot silently disable a guard.

## Known limits

- **Restore point is not a full guarantee** (see *Backup*). Open.
- **A proposal must carry every token of the masked conflict.** The four texts of one conflict
  share one token table, so a proposal that drops a token from any side is refused, even when the
  drop was correct. The person chooses by hand in that case. This is deliberately strict.
- **The confirmation is the only gate on the endpoint.** The predicate counts a host outside the
  built-in rule as public, so a private host the config does not name is confirmed once, which is
  the safe direction.
- **An `openai` error can echo part of the key** if the library puts the header in its message.
  The library is not under this project's control.
- **XML validation is not available.** Only the JSON and zsh checks exist, so an XML suggestion has
  no syntax check beyond the merge's own render round-trip. XML waits for the XML adapter.

## Where it lives

| Concern | Module |
| --- | --- |
| Token format, strict answer, restore, URL trust, endpoint predicate | `core/advice.py`, `core/presets.py` |
| Masking | `system/masking.py` |
| Model client (lazy `openai`, no redirects) | `llm/client.py` |
| API key | `llm/keys.py` |
| Advised chooser, pre-check, auto-apply policy | `services/resolve.py` |
| `--llm`, the confirmation, the presenter | `cli/main.py` |
