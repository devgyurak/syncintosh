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
- **The conflict carries secrets.** A conflict's sides may hold tokens, internal hostnames, paths
  that reveal the person, and their username. The masker removes the ones it recognises before the
  model sees them. It does not recognise everything (see *What leaves the machine*).

## What leaves the machine

For one content conflict, the model receives five texts: the file path, the key path inside the
file (empty for a whole file), and the base, local and remote values. Before they leave, the masker
(`system/masking.py`) replaces the recognised values with tokens:

- every secret pattern the scanner knows, both its structured and its shell table, so a credential
  the scanner would refuse is masked here too;
- a value written under a credential-like key: `password`, `passwd`, `pwd`, `secret`, `token`,
  `api_key`, `apikey`, `credential`, `private_key`, in JSON, shell, and YAML spellings;
- the home directory path, and the username as a whole word;
- internal hostnames and IP addresses, and the host of any URL, by the built-in rule (`.local`,
  `.internal`, `.lan`, `.corp`, `home.arpa`, `localhost`, a single-label name, loopback, link-local,
  private and CGNAT IPs) and by the domains the config lists in `llm.internal_domains`.

**This is not "every value".** A value under a key name outside that list, in a format the patterns
do not recognise, is sent as it is. The model needs the values to merge them: a conflict with every
value masked gives it nothing to choose between. The trade-off is deliberate, and it is why the
approval screen shows what will be published, in full. Narrowing it further is a decision for the
person who owns the settings, not a default.

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
- **A content conflict is applied without asking only when all four hold:** its preset is listed
  in `llm.auto_apply` (for example `["vscode"]`); the preset is not a shell preset; the answer is a
  usable choice that passes the check; and the proposal introduces **no new endpoint** (no new URL,
  and no host the three sides did not name) and **leaves out no token** of the masked conflict.
  A shell preset is refused in `llm.auto_apply` at `resolve --llm`: a shell file is code the next
  shell runs, so a change to it is always asked.
- **Every other suggestion is shown,** and the person accepts or declines it. A declined or refused
  suggestion falls back to the person's own choice for that conflict.
- **The final approval always runs,** unless `--yes` is given. It shows every pick the run would
  publish, including the ones the model proposed.

## URLs and hosts

A proposal can introduce an endpoint in any spelling: a URL with a scheme, in any letter case or
JSON escape; a `git@host:` remote; a `HTTP_PROXY=host:port`; a `//host` form; a `curl ftp://host`
pipe. A URL is *new* when none of the base, local or remote values already held it, and a host is
*new* when none of them named it. Every new endpoint is:

- shown with its domain, and labelled `UNTRUSTED <domain>`;
- **never trusted**, even when its text matches a URL a preset ships. Trust comes only from a
  shipped preset's exact download URL (`download_trust`, `ShippedPresets`), and a proposal is not a
  shipped preset.
- a reason the proposal is always asked, even for an auto-applied preset.

A host written as an all-numeric name is read as the address the resolver reads it:
`134744072` and `0x08080808` are 8.8.8.8, which is public, and `127.1` is loopback. The endpoint
check and the plain-http refusal use that reading, so a public address cannot pass as local by its
spelling.

## Binary files

A binary record is never settled by a text proposal, and never sent to the model. The record's
format decides first; a text record whose bytes do not decode as UTF-8 is treated the same way. The
check is in the sync service, so it holds for every resolve path, not only the model's.

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
- **Values outside the masking rules are sent.** See *What leaves the machine*. Open as a design
  decision: masking every value would leave the model nothing to merge.
- **A dropped value is asked for, not applied silently.** A proposal may leave a token out, so it may
  drop a value the local side had. Such a proposal is always shown, even under `auto_apply`, and the
  approval screen shows the proposed text and the diff against the local side.
- **The confirmation is the only gate on the endpoint.** The predicate counts a host outside the
  built-in rule as public, so a private host the config does not name is confirmed once, which is
  the safe direction.
- **The `openai` error stays chained.** Our own error text is fixed, so it does not repeat the
  library's message; the library's exception is kept as the cause, and its message may echo part of
  the request if the library puts it there. The library is not under this project's control.
- **The openai 1.x branch is not in CI.** The lock file pins openai 3.x, so CI exercises only the
  httpx2 client. The 1.x path (httpx) was run by hand against a loopback server: a normal reply, a
  refused redirect, and an empty answer. Pinning openai to 3.x in `pyproject.toml` would remove the
  branch; it is waiting for a decision.
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
