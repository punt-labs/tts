# Provider authority — state decides, voxd obeys or refuses

**Bead:** vox-w3f8 (P1 bug). **Parent epic:** vox-awm9 (provider → model →
voice cascade and state authority). This is the state-authority half.

**Type:** design. No code in this document's change; the write set in §6 is a
recommendation for the implementation mission, not a prescription carried over
from the bead.

**Forward-integrated by contract.** Every path this design retires is deleted
in the same commit as its replacement. There is no shim, no flag, no
warn-and-continue, no deprecation window. Punt Labs products have no installed
base, so there is nothing to migrate and nothing to bridge to.

---

## 1. The defect

### 1.1 What was reported

A session configured for `polly`, on a host with no Polly credentials loaded
into the daemon. `mic:status` returned `{"provider": "polly"}`. `vox.log`
recorded `Synthesize: provider="openai" voice="bella"`. `bella` is an
ElevenLabs voice and is not in the OpenAI roster, so synthesis failed with
`VoiceNotFoundError` — and the caller was told `polly` throughout.

Three separate things went wrong. They are separable and each has its own
citation.

### 1.2 The substitution: voxd invents a provider when the wire carries none

`voxd` fills an absent provider by probing the environment:

- `voxd/speech_handlers.py:70` — `provider=parse_optional_str(msg,
  "provider") or auto_detect_provider()`, on the synthesize/record path.
- `voxd/system_handlers.py:94` — the same expression on the `voices` path.
- `voxd/health.py:96–99` — `payload["provider"] = auto_detect_provider()`.

`auto_detect_provider` is `ProviderRegistry.auto_detect`
(`providers/__init__.py:125–188`). Its order is `TTS_PROVIDER` env var,
then `ELEVENLABS_API_KEY`, then `OPENAI_API_KEY`, then an `aws sts
get-caller-identity` subprocess, then the platform binary, and finally
`polly` with `detected=False` (`providers/__init__.py:154–169`). On the
reported host, `OPENAI_API_KEY` was set, so the probe answered `openai`.

The voice travelled independently. `hooks.py:167–169` sends `voice=config.voice
or None, provider=config.provider or None` — so a `vox.md` carrying
`voice: bella` with no usable `provider:` value produces exactly the reported
log line: the voice from state, the provider from the probe.

`ProviderRegistry.get` has a second substitution of the same species
(`providers/__init__.py:103–108`): when `name` is `None` it reads a config
file and, failing that, calls `auto_detect()`. All three daemon call sites
pass `config_dir=None` (`voxd/synthesis.py:309`, `voxd/synthesis.py:406`,
`voxd/system_handlers.py:96`), and `DEFAULT_CONFIG_DIR` is the *relative*
path `.punt-labs/vox` (`dirs.py:13`), so the daemon resolves it against its
own working directory. A `voxd` started from inside a repo therefore reads
that repo's `vox.md` on behalf of every client, whatever repo the client is
in. That is a second state-authority defect in the same function, latent
today only because the launchd/systemd daemon runs from `/`.

### 1.3 Four more silent substitutions of the same class

The probe is not the only place vox answers a question about the provider by
making one up.

| Site | Substitution |
|------|--------------|
| `server_switches.py:132` | `provider = session.provider or "elevenlabs"` — `mic:model` with no provider configured lists ElevenLabs models. |
| `server_switches.py:316–321` | `_list` fetches the roster for `session.provider` (`None` → the daemon probe picks one) and then labels the result `session.provider or "elevenlabs"`. The roster and the label can name different providers. |
| `commands/model.py:42` | the same `cfg.provider or "elevenlabs"` on the CLI. |
| `panel/service.py:189` | `MODEL_TABLE.available(provider or "elevenlabs")`. |
| `panel/service.py:329` | the voice preview sends `SynthesisSpec(voice=voice)` with no provider at all — an unconditional trigger of the daemon probe while the panel's own state holds a provider. |
| `__main__.py:354–367` | `vox say` sends only `--provider`; it never fills from state, so a bare `vox say "hi"` ignores `vox.md` entirely for both provider and voice. |
| `resolve.py:94–106` | on `VoiceNotFoundError`, if the voice came from config, log INFO and use the provider default instead. This function has no production callers — only `tests/test_resolve.py` — but it is the same pattern preserved as a template. |

`server.py:312–317` explains how status and the wire diverged: `refresh_from_config`
overwrites `_provider` only when the config file holds a non-`None` value, so an
in-memory session provider set by a tool survives a config that never recorded
it. `mic:status` reports `_session.provider` (`server.py:648`) — the in-memory
value — while `hooks.py:168` reads the file. Two readers, two answers.

`server_audio_tools.py:233–244` (`rec new`) is the one synthesis surface that
already fills the provider from session state correctly.

### 1.4 The caller is told nothing useful when it does fail

`WireReply.fault` sends `SafeFault.wire_message` (`wire_reply.py:98–103`).
`SafeFault._wire_for` (`wire_fault.py:95–102`) returns a relativized message
only for an in-jail `OSError`; everything else becomes the literal string
`"operation failed"`. `_synthesize_and_enqueue` catches `except Exception`
and routes to `fault` (`speech_handlers.py:229–233`), so today:

- a missing OpenAI key (`openai.OpenAI()` raises at construction —
  `providers/openai.py:63`) reaches the caller as `"operation failed"`;
- `NoCredentialsError` from Polly at call time reaches the caller as
  `"operation failed"`;
- the `VoiceNotFoundError` from the reported incident
  (`providers/openai.py:201`) reaches the caller as `"operation failed"`.

`SafeFault` is a trust-boundary guard against leaking an absolute path prefix,
and it is right about paths. It is being used as a catch-all, which turns
every diagnosable failure into an undiagnosable one.

`VoicesHandler` has the same shape: it catches `(ValueError, LookupError,
OSError)` (`system_handlers.py:98`) and then `except Exception`
(`system_handlers.py:103–108`). `botocore.exceptions.NoCredentialsError` is in
neither the first tuple nor an `OSError`, so `mic:provider polly` on a host
without AWS credentials already fails today — with the message
`"operation failed"`, by way of `Cascade.fetch_roster`
(`cascade.py:129–135`) and `RosterError`.

### 1.5 The three providers fail three different ways

Verified on this host with the keys unset (`uv run python`, the ElevenLabs and
OpenAI keys popped from `os.environ`):

```text
elevenlabs: constructed OK without key
openai: raised OpenAIError Missing credentials. Please pass an `api_key`, ...
polly: client constructed OK
```

So the failure point moves with the provider: OpenAI at construction
(`providers/openai.py:63`), ElevenLabs at the first API call
(`providers/elevenlabs.py:78–84`), Polly at the first API call
(`providers/polly.py:155–168`), espeak at construction with a clear message
(`providers/espeak.py:103–110`). There is no single moment today at which
"this provider cannot run here" is known, which is why the answer has to be
put somewhere deliberately rather than left to the SDKs.

---

## 2. The rule

Settled by the operator; this document implements it and does not reopen it.

1. **State on disk is the source of truth.** If state says `provider: polly`,
   voxd tries Polly.
2. **No substitution, ever.** voxd never runs a provider other than the one it
   was given. Not on a missing credential, not on a missing binary, not on an
   unknown name.
3. **Refusal is loud and specific.** The failure names the missing credential
   and points at `vox doctor`.
4. **`mic:status` reports what voxd will actually run with.** Under rules 1–3
   that equals what state declares, so the two cannot diverge.

One corollary, which the rest of the design leans on:

> Detection may **propose** a value to be written into state. It may never
> **stand in** for state at run time.

That is the line between `vox enable` picking a sensible starting provider and
writing it into `vox.md`, and `voxd` guessing on every request. The first is a
recorded, visible, reviewable decision. The second is the bug.

---

## 3. The resolution design

### 3.1 Where the error surfaces, and why

Three candidate points were considered. The choice is **provider resolution**,
in the daemon, at the single function that turns a provider name into a
provider object.

**Rejected — daemon start.** `voxd` is one per-user process shared by every
repo, with no session and no project context. At start it cannot know any
session's provider, and refusing to start because one repo's `vox.md` names an
uncredentialed provider would silence every other repo. It is also stale by
construction: `keys.env` is read exactly once, at start
(`voxd/config.py:155–185`), so a start-time verdict would not see a later
`vox daemon install`, an AWS SSO refresh, or an expiry. Decisively, a
mid-session `mic:provider polly` happens long after start — the gate would
never fire for the case the bead is about.

**Chosen — provider resolution (`ProviderRegistry.get`,
`providers/__init__.py:80`).** It is the one funnel: all three daemon paths
reach it (`voxd/synthesis.py:309` for synthesize, `voxd/synthesis.py:406` for
direct play, `voxd/system_handlers.py:96` for the voice roster), and nothing
outside the daemon calls it. It sits before every cost: before the provider
SDK is constructed, before any billable API call, before the temp file
(`voxd/synthesis.py:323`), before the playback queue, before `cache_put`. It
is cheap enough to run per request — an environment lookup and, for Polly, a
local botocore credential resolution — so its verdict is current rather than
cached. And because the roster path and the synthesis path share it,
`mic:provider`, `mic:voice`, and `mic:unmute` cannot disagree about whether a
provider can run.

It must sit *inside* the API-key context, not outside it:
`_api_key_context` injects a per-call `api_key` into `os.environ` around the
provider construction (`voxd/synthesis.py:59–74`, applied at
`voxd/synthesis.py:308` and `voxd/synthesis.py:124`). Putting the check inside
`get_provider` — which is called within that context — means a caller who
supplies `api_key=` for a provider with no ambient credential is correctly
allowed through.

**Rejected as the primary gate — synthesis time.** By then the request is
built, the env lock is held, and the failure arrives as whichever SDK
exception the provider happens to raise, at whichever moment that provider
happens to raise it (§1.5). Credentials that are *present but rejected* (a
revoked key, an expired token) genuinely cannot be caught earlier without a
network probe per call, so that class stays here — but it stays as a residual,
with its own typed error, not as the place the whole question is answered.

### 3.2 What a mid-session provider switch looks like under each choice

This is the case that decides it. The caller runs `mic:provider "polly"` on a
host with no AWS credentials in the daemon.

| Gate | What the caller experiences |
|------|------------------------------|
| Daemon start | Nothing fires. The switch writes `polly` to the tracked `vox.md`, `mic:status` says `polly`, and the next hook chime either substitutes (today's bug) or fails opaquely. The gate is blind to every event after boot. |
| **Provider resolution** | The switch itself resolves the provider: the cascade fetches the new provider's voice roster (`server_switches.py:232` → `cascade.py:138–148` → `client.voices` → `system_handlers.py:96` → `get_provider`). The gate fires there. `mic:provider` returns `{"error": "provider 'polly' is configured but voxd has no AWS credentials …"}` and returns **before** `write_fields` (`server_switches.py:236–240`), so nothing is written. The session stays on the working provider, `mic:status` keeps telling the truth, audio keeps working, and the unusable state is unreachable through the switch surface. |
| Synthesis time | The switch succeeds. `polly` lands in `vox.md` — which is a **tracked** durable key (`config.py:35–43`), so the broken state can be committed and handed to a teammate. `mic:status` says `polly`, truthfully. Every subsequent synthesis fails, one at a time, and the repo is mute until somebody reads an error. |

Two further switch cases, for completeness:

- **Credentials disappear mid-session** (SSO expiry, a rewritten `keys.env`).
  Resolution catches it on the next request, before spending anything, with
  the same message. Start-time cannot see it; synthesis-time turns it into an
  SDK error.
- **Switch races an in-flight synthesis.** No hazard. Each request carries its
  own provider on the wire (`voxd/speech_handlers.py:68–82`) and the daemon
  constructs a provider per call and discards it
  (`providers/__init__.py:118–123`). The in-flight synthesis finishes on the
  provider it started with; the next request uses the new one. There is no
  shared mutable provider state in the daemon to corrupt.

### 3.3 The daemon stops reading state and stops inventing it

`ProviderRegistry.get` loses both of its guesses. Its signature becomes:

```python
def get(self, name: str, *, model: str | None = None) -> TTSProvider: ...
```

- No `name: str | None`. A caller must name a provider.
- No `config_dir` parameter and no `ConfigStore` read
  (`providers/__init__.py:99–101, 113–117` deleted). The daemon has no
  project context by design (`CLAUDE.md`, "Key architectural boundary"), and
  the relative-path read described in §1.2 goes with it. Model, like
  provider, arrives on the wire from the client that owns the state.
- No `auto_detect()` branch (`providers/__init__.py:108`).

`auto_detect` itself is not deleted, but it is renamed to say what it is now
allowed to do — `ProviderRegistry.propose()` / `propose_provider()` — and its
callers are reduced to the two that write a value into state rather than
substitute for one:

- `vox enable` seeds `provider` into `vox.md` once, at enable time (new; see
  §3.6).
- `desktop_install.py:65–73` picks the `TTS_PROVIDER` written into the Claude
  Desktop registration.

Both are recorded, visible decisions. `TTS_PROVIDER` (`keys.py:29`,
`providers/__init__.py:154–156`) therefore stops being able to override state
at run time; it is an input to a proposal, nothing more. Without this the same
bug returns wearing an environment variable.

### 3.4 One object owns "can this provider run here"

The knowledge is currently in four partial copies:
`providers/__init__.py:157–188` (the probe), `voxd/synthesis.py:53–56`
(`_PROVIDER_API_KEY_VAR`), `desktop_install.py:38–41` (`_PROVIDER_KEY_VARS`),
and `keys.py:20–32` (the flat union of variable names). None of them covers
all five providers and they cannot be kept in step by hand.

Consolidate into one class in a new module, `providers/credentials.py`.
The per-provider requirement is *behaviour*, not a row in a table — an API key
for two providers, an AWS credential chain for one, an executable on `PATH`
for two — so it dispatches on the provider rather than branching on its name
(PY-OO-6, `oo.md` "polymorphism over conditionals"):

```python
class CredentialRequirement(Protocol):
    def satisfied(self) -> bool: ...
    def unmet_message(self, provider: str) -> str: ...
```

with `ApiKeyRequirement("ELEVENLABS_API_KEY")`,
`ApiKeyRequirement("OPENAI_API_KEY")`, `AwsRequirement()`,
`BinaryRequirement("say")`, and `BinaryRequirement("espeak-ng", "espeak")`.

`AwsRequirement` uses `boto3.Session().get_credentials() is not None`, not the
`aws sts get-caller-identity` subprocess at `providers/__init__.py:171–188`.
The subprocess costs up to five seconds, needs the `aws` CLI on `PATH`, and
answers a different question (are these credentials *valid*) than the one the
gate asks (are there credentials for boto3 to use). `get_credentials()`
consults the same chain boto3 will use at synthesis — environment, profile,
config file, instance role — with no network call, which is verified: on this
host the call returns a credential object and does so immediately.

`voxd/synthesis.py:53–56` and `desktop_install.py:38–41` are deleted and their
callers re-pointed here; `keys.PROVIDER_KEY_NAMES` is derived from the same
object so `keys.env` and the gate cannot drift.

The class has exactly two entry points, and both callers get the same answer
from the same code:

- `require(provider)` — raises `ProviderUnavailableError` when unmet. Called
  by `ProviderRegistry.get`.
- `report(provider)` — returns the verdict without raising. Called by the
  `provider_status` wire op (§3.5).

Status therefore cannot drift from behaviour: they are one function, called
two ways.

### 3.5 The error type, and how it reaches the caller

The type is a `ValueError` subclass in `types_errors.py`, alongside
`ConfigValueError` and `VoiceNotFoundError`:

```python
class ProviderUnavailableError(ValueError):
    """The named provider is configured but cannot run on this daemon."""
```

The `ValueError` base is load-bearing. `WireReply.reject_or_fault`
(`wire_reply.py:105–123`) already routes a `ValueError` to `error()`, whose
frame carries the message **verbatim** (`wire_reply.py:79`), while everything
else goes to `fault()` and is laundered to `"operation failed"`. "Your state
names a provider this daemon cannot run" is a rejected request, not a daemon
malfunction, so the existing taxonomy is already correct — the change is to
raise the right type and stop swallowing it in a broad `except Exception`.

Two handler changes make that real:

- `speech_handlers.py:229–233` catches `ProviderUnavailableError` and
  `VoiceNotFoundError` before the broad guard and routes them to
  `WireReply.error`. `_SpeechRequest` gains a `reject(message)` sibling to its
  existing `reply` and `fault` (`speech_handlers.py:90–109`).
- `system_handlers.py:98` adds nothing — `ProviderUnavailableError` is a
  `ValueError` and the existing `reject_or_fault` call already classifies it
  correctly.

Message text carries the variable names, never a value (PL-PP-4), and never a
filesystem path: `keys.env` lives under `$HOME`, and an absolute prefix must
not cross to a client — which is precisely what `SafeFault` exists to prevent.
That is why the message points at `vox doctor`, which runs host-local and may
print the path.

```text
provider 'polly' is configured but voxd has no AWS credentials
  (AWS_PROFILE, or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY); run `vox doctor`
provider 'elevenlabs' is configured but voxd has no ELEVENLABS_API_KEY;
  run `vox doctor`
provider 'espeak' is configured but neither espeak-ng nor espeak is on voxd's
  PATH; run `vox doctor`
```

For that pointer to be worth anything, `vox doctor` has to answer. Today it
does not: it prints `(provider: <auto-detected>)` from the health payload
(`doctor.py:261, 276–281`) and its only credential-adjacent check reads the
*caller's* environment (`doctor.py:228–244`), which is the wrong process —
the daemon's environment is the one that matters. So:

- `health.provider` is deleted (`voxd/health.py:96–99`, `types_health.py:28,
  45`) — the daemon has no provider of its own to report, and reporting the
  probe's answer there is the same lie in a third place.
- `doctor.py` gains a per-provider readiness section fed by the new wire op,
  reporting the daemon's verdict for all five providers.

### 3.6 What `mic:status` reports, and whether it needs a new field

**The existing `provider` field becomes authoritative on its own.** It already
reports what state declares (`server.py:648`); the divergence disappears when
the daemon stops substituting. No change is needed to make it *true*.

**One new field is needed anyway, because true is not the same as
sufficient.** A caller must be able to learn that the declared provider cannot
run *without first attempting a synthesis*, and the hook path has no return
channel at all — a Stop-hook chime that refuses has nowhere to report except
the log, which is exactly the outcome this project forbids. Status is the only
surface where "your notifications are silently failing" can be observed.

So `mic:status` gains a daemon-authoritative `provider_status` block, shaped
and handled exactly like the music `program` block it sits beside — fetched
fresh from voxd on every call, never cached server-side, with an
`unavailable` form when the daemon is unreachable (`server.py:660–664`,
`MusicStateView.unavailable`). This is the `mic:status` extension the epic
design already anticipated and deferred
(`docs/vox-0rp9-model-provider-voice.md:798–803`).

```json
{
  "provider": "polly",
  "provider_status": {
    "name": "polly",
    "ready": false,
    "reason": "no_credentials",
    "detail": "voxd has no AWS credentials (AWS_PROFILE, or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY); run `vox doctor`"
  }
}
```

`reason` is a closed set — `Literal["ok", "unconfigured", "unknown_provider",
"no_credentials", "voxd_unavailable"]` — not a free string, so a caller can
branch on it and an illegal value cannot be written.

The daemon side is a new wire op, `provider_status`, with an optional
`provider` field: named, it reports one; absent, it reports all five (which is
what `vox doctor` wants). It gets its own module,
`voxd/provider_status_handler.py`, because `voxd/system_handlers.py` already
holds three classes and PY-OO-2 caps a module at three.

### 3.7 The client half: state must always send a provider

With the daemon's guess gone, the burden moves to the clients, and today five
of them build a spec independently — `server.py:241–249` (`fill_defaults`),
`server_audio_tools.py:233–244`, `hooks.py:167–169`, `__main__.py:354–367`,
`panel/service.py:329`. Three of the five are already wrong in the ways §1.3
lists. Fixing this five times guarantees the sixth surface reintroduces it.

Extract one object — `session_spec.py` — that turns a `VoxConfig` into a
`SynthesisSpec` whose provider is guaranteed non-empty, and raises
`ProviderNotConfigured` when state declares none. Every synthesis surface
calls it. `SessionConfig.fill_defaults` becomes a thin delegation to it, and
the CLI and the panel acquire the state-filling they never had.

`SynthesisSpec.provider` stays `str | None` (`types_synthesis.py:30`): it is
the *request* shape, where a per-call override is legitimately unset before
filling. The guarantee lives in the one constructor that all surfaces use, and
is enforced a second time at the wire boundary, where `_SpeechRequest.from_msg`
parses `provider` with `parse_required_str` instead of
`parse_optional_str(...) or auto_detect_provider()`
(`voxd/speech_handlers.py:70`). A hand-rolled client that omits the field gets
`missing required field 'provider'` — a rejection, correctly attributed.

`client.py:909–917` (`voices(provider=None)`) likewise requires the provider,
which incidentally fixes the roster-vs-label mismatch at
`server_switches.py:316–321`.

### 3.8 An empty provider in state is a refusal, not a licence to guess

`provider: ""` is the shipped shape of a fresh `vox.md` (`DESIGN.md:69`),
and `VoxConfig.provider` faithfully reports it (`config.py:127`). Empty state
declares nothing, so nothing is contradicted by refusing — and guessing here
is the same bug with a different trigger.

Refusing alone would leave a freshly enabled repo mute until someone ran
`vox provider`, which is a real cliff and the strongest argument the fallback
ever had. The answer is to make the empty state unreachable in the normal
path rather than to guess out of it: **`vox enable` / `mic:enablement
action="enable"` writes a real provider into `vox.md`**, chosen by
`propose_provider()` at that moment, and reports which one it chose. Detection
survives exactly once, where a human is present, and its result lands in state
where `mic:status` shows it and git records it.

A repo that still reaches synthesis with no provider — a hand-edited file, an
older `vox.md` — gets the client-side refusal in §4, F1.

---

## 4. Failure modes and their client-observable surface

Every row states what the caller sees through the API. The log line is
additionally required in every row, for the audit trail; it is never the
channel a client is asked to read.

### F1 — state declares no provider

Owner: the client (the daemon cannot know what repo you are in).

- `mic:unmute`, `mic:rec new`: `{"error": "no TTS provider is configured for
  this repo; set one with mic:provider <name>"}` — the existing
  `SegmentBatch` envelope (`synthesis_batch.py:75–86`).
- `mic:status`: `"provider": null`, `provider_status.reason ==
  "unconfigured"`.
- `mic:provider`, `mic:model`, `mic:voice` with no argument: list normally
  with `"current": null` — listing does not require a provider. `mic:model`
  and `mic:voice` no longer substitute `"elevenlabs"`
  (`server_switches.py:132, 321`, `commands/model.py:42`,
  `panel/service.py:189`).
- CLI: the message on stderr, exit 1.
- Hooks: no audio, one WARNING in `vox.log`. A hook has no caller to answer,
  which is why `provider_status` exists.
- Panel: the notice band, via the existing `PanelNotice` path.

### F2 — state declares a provider; the daemon has no credentials for it

Owner: the daemon. This is the bead's case.

- Wire — raised at resolution, sent verbatim through `WireReply.error`
  (`wire_reply.py:60–79`):

  ```json
  {"type": "error", "message": "provider 'polly' is configured but voxd has no AWS credentials (AWS_PROFILE, or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY); run `vox doctor`"}
  ```

- `mic:unmute`: `{"error": "<that text>"}` — the client turns the error frame
  into `VoxdProtocolError` (`client.py:362–363`) and `SegmentBatch` renders
  the envelope (`synthesis_batch.py:58–62`).
- `mic:provider "polly"`: the same text, and **no write** — the state stays
  usable (§3.2).
- `mic:voice` (no argument): the same text, via `RosterError`
  (`cascade.py:129–135`).
- `mic:status`: `provider_status.reason == "no_credentials"` with the same
  `detail`.
- `vox doctor`: a failing line for that provider, and a passing line for each
  provider that is ready.
- Log: `ERROR ... rejected op id=... provider 'polly' is configured but ...`

Nothing is spent: no SDK client is built, no API call is made, no temp file is
written, no cache entry is created.

### F3 — credentials are present but the provider rejects them

Owner: the provider. A revoked key, an expired token, a wrong account. Not
catchable at resolution without a network round trip per request, so it stays
at synthesis — but it stops being `"operation failed"`.

- The provider layer raises a typed `ProviderAuthError` carrying the provider
  name and the status code, and the synthesize handler routes it to
  `WireReply.error` like F2. `mic:unmute` shows:

  ```json
  {"error": "provider 'elevenlabs' rejected the credentials (HTTP 401); run `vox doctor`"}
  ```

- `mic:status` reports `ready: true` — which is honest. Presence is a cheap
  local fact; validity is a network fact. The live probe belongs to
  `vox doctor`, which already has per-provider `check_health` implementations
  to build on (`providers/polly.py:245–291`,
  `providers/openai.py:122–170`).

Status is deliberately not given a "last error" field. `voxd` is shared by
every repo and holds no session, so a daemon-wide last-error would report one
repo's failure to another. The failing call answers its own caller; status
answers the standing question.

### F4 — state names a provider that does not exist

A hand-edited `provider: ploly`. `mic:provider` cannot produce it (the
`Literal` schema narrows it, `server_switches.py:53`) but the file can.

- `ProviderRegistry.get` already raises `ValueError("Unknown provider
  'ploly'. Available: …")` (`providers/__init__.py:118–122`). It is a
  `ValueError`, so once the broad `except Exception` in
  `speech_handlers.py:229` stops swallowing it, that text crosses verbatim.
- `mic:status`: `provider_status.reason == "unknown_provider"`.

### F5 — the voice is not in the provider's roster

The reported incident's proximate failure, and today also `"operation
failed"`.

- `VoiceNotFoundError` (`types_errors.py:25`) is a `ValueError` and crosses
  verbatim once the handler stops laundering it: `{"error": "bella
  (available: alloy, ash, ballad, …)"}`.
- This is a genuine improvement carried by the same change, not scope creep:
  it is the second half of the log line in the bead.

### F6 — voxd is unreachable

- `mic:status`: `provider_status.reason == "voxd_unavailable"`, mirroring
  `MusicStateView.unavailable` (`server.py:661–663`).
- Synthesis surfaces: the existing `VoxdConnectionError` envelope, unchanged.

---

## 5. What is deliberately not built

- **No fallback in any form.** No warn-and-continue, no try-then-fallback, no
  "best effort", no environment variable or flag that restores the old
  behaviour. The probe survives only as a *proposal* at enable and desktop
  registration (§3.3), where its result is written into state.
- **No migration.** `provider: ""` in an existing `vox.md` is handled by the
  refusal in F1 and by enable writing a real value; there is no detector, no
  one-shot upgrade path, no `vox provider --migrate`.
- **No readiness cache in the daemon.** The check is cheap enough to run per
  request, and a cache would need invalidation — which is how a stale verdict,
  the very thing this bead is about, gets reintroduced.

---

## 6. Recommended write set

The design mission owns the write set; this is it. The implementation mission
may split it across at most two rollback-coherent PRs (the daemon gate and the
observability surface), but they are sequentially dependent in that order.

| File | Change | Why |
|------|--------|-----|
| `src/punt_vox/providers/credentials.py` | **new** — `CredentialRequirement` protocol, its five implementations, and the `ProviderCredentials` facade with `require` / `report`. | One home for "can this provider run here", replacing four partial copies. |
| `src/punt_vox/providers/__init__.py` | `get(name: str, *, model)` — drop `None`, the config read, and the auto-detect branch; call `ProviderCredentials.require` before the factory; rename `auto_detect` → `propose`. | The substitution and the daemon's config read both live here. |
| `src/punt_vox/types_errors.py` | add `ProviderUnavailableError`, `ProviderNotConfigured`, `ProviderAuthError`. | `ValueError` subclasses so the existing reject-vs-fault taxonomy carries their text verbatim. |
| `src/punt_vox/types_provider.py` | **new** — `ProviderReadiness` (frozen, `from_wire`) and the `reason` `Literal`. | The wire shape both the daemon and the client need, importable without heavy deps. |
| `src/punt_vox/voxd/provider_status_handler.py` | **new** — the `provider_status` op, one or all providers. | A fourth class will not fit in `system_handlers.py` (PY-OO-2). |
| `src/punt_vox/voxd/handler_registry.py` | register `provider_status`. | Wiring. |
| `src/punt_vox/voxd/speech_handlers.py` | require `provider` on the wire; add `_SpeechRequest.reject`; catch the typed provider errors and `VoiceNotFoundError` before the broad guard. | Removes the synthesize-path guess and stops laundering diagnosable errors. |
| `src/punt_vox/voxd/system_handlers.py` | require `provider` on the `voices` op; delete the `auto_detect_provider` import. | Removes the roster-path guess. |
| `src/punt_vox/voxd/health.py` | delete `payload["provider"]`. | The daemon has no provider to report. |
| `src/punt_vox/types_health.py` | delete the `provider` field and its `from_wire` line. | Follows the payload. |
| `src/punt_vox/voxd/synthesis.py` | delete `_PROVIDER_API_KEY_VAR`; take the key-var map from `providers/credentials.py`. | De-duplicates the credential table. |
| `src/punt_vox/session_spec.py` | **new** — `VoxConfig` → `SynthesisSpec` with a guaranteed provider, raising `ProviderNotConfigured`. | One state-to-spec constructor instead of five, so the next surface cannot reintroduce the bug. |
| `src/punt_vox/server.py` | `fill_defaults` delegates to `session_spec`; `status()` merges the `provider_status` block. | The MCP surface. |
| `src/punt_vox/server_switches.py` | drop `or "elevenlabs"` at `:132` and `:321`; label the roster with the provider actually fetched. | Two more silent substitutions. |
| `src/punt_vox/commands/model.py` | drop `or "elevenlabs"`. | The CLI twin of the same substitution. |
| `src/punt_vox/hooks.py` | build the spec through `session_spec`. | Hooks were the path that produced the reported log line. |
| `src/punt_vox/__main__.py` | `say` / `record` fill from state through `session_spec`. | `vox say` ignores `vox.md` today. |
| `src/punt_vox/panel/service.py` | preview fills the provider; drop `or "elevenlabs"` at `:189`. | The preview sends no provider at all today. |
| `src/punt_vox/client.py`, `client_sync.py` | `voices(provider: str)` required; add `provider_status(...)`. | The client half of both ops. |
| `src/punt_vox/cascade.py` | `fetch_roster(client, provider: str)`. | Propagates the required provider. |
| `src/punt_vox/enablement.py` | write a proposed `provider` into `vox.md` at enable; report it. | Makes the empty state unreachable in the normal path. |
| `src/punt_vox/doctor.py` | drop `(provider: …)`; add a per-provider readiness section from the new op; drop the caller-env check at `:228–244`. | The error message points here, so it has to answer. |
| `src/punt_vox/desktop_install.py` | use `providers/credentials.py`; call `propose_provider`. | Deletes the third copy of the key-var map. |
| `src/punt_vox/keys.py` | derive `PROVIDER_KEY_NAMES` from `providers/credentials.py`. | Deletes the fourth. |
| `src/punt_vox/resolve.py` | delete `resolve_voice_and_language` and `_validate_and_infer`. | No production callers, and it is the same silent-substitution pattern preserved as a template (PY-RF-6, forward integration). |
| `tests/test_resolve.py` | delete. | Follows its subject. |
| `tests/…` | per §7. | |
| `CHANGELOG.md`, `README.md`, `DESIGN.md` | Changed/Fixed entry; the `mic:status` shape; an ADR recording the authority rule and the rejected gate positions. | Documentation discipline. |

---

## 7. Test plan

Named cases. Each is a test that fails before the change.

**The bead's case, end to end.**

1. `polly_configured_without_credentials_refuses` — state declares `polly`,
   `AwsRequirement` unsatisfied. The synthesize op returns an error frame
   naming the AWS variables. No provider is constructed, no temp file is
   written, `cache_put` is not called.
2. `polly_configured_without_credentials_never_substitutes` — the same setup
   with `OPENAI_API_KEY` set in the daemon environment. Assert the reply is
   the refusal and that no OpenAI provider was constructed. This is the
   regression test for the exact reported log line.
3. `status_matches_what_voxd_would_run` — `mic:status` reports
   `provider: "polly"` and `provider_status.ready is False`; the synthesize
   attempt refuses with the matching text. One property, asserted across the
   two surfaces.

**Resolution gate.**

1. `get_requires_a_provider_name` — `get(None)` no longer type-checks and
   `get("")` raises.
2. `get_does_not_read_config` — a `vox.md` in the daemon's cwd naming a
   different provider changes nothing.
3. `per_call_api_key_satisfies_the_requirement` — no ambient
   `ELEVENLABS_API_KEY`, `api_key=` supplied on the call, synthesis proceeds.
   Guards the check's placement inside `_api_key_context`.
4. One case per requirement: missing `ELEVENLABS_API_KEY`, missing
   `OPENAI_API_KEY`, unresolvable AWS chain, `say` absent from `PATH`,
   neither espeak binary present. Each asserts the exact message and that the
   message contains no absolute path.

**Wire taxonomy.**

1. `provider_unavailable_crosses_verbatim` — the message is not
   `"operation failed"`.
2. `voice_not_found_crosses_verbatim` — the second half of the reported
   incident.
3. `absent_provider_field_is_a_rejection` — a wire message with no
   `provider` gets `missing required field 'provider'`, logged as a rejected
   op, not a fault.
4. `path_bearing_oserror_still_relativizes` — `SafeFault` behaviour is
   unchanged for the case it exists for.

**Switch.**

1. `switch_to_uncredentialed_provider_writes_nothing` — `mic:provider polly`
   errors and `vox.md` is byte-identical afterwards.
2. `switch_to_credentialed_provider_still_cascades` — the model/voice cascade
   is untouched.
3. `in_flight_synthesis_keeps_its_provider` — a switch during a synthesis
   does not change the running request's provider.

**Client state filling.**

1. `vox_say_uses_configured_provider` — a bare `vox say` sends the provider
   from `vox.md`.
2. `panel_preview_uses_configured_provider`.
3. `hook_speech_uses_configured_provider`.
4. `unconfigured_provider_refuses_on_every_surface` — one parameterised test
   across MCP, CLI, hook, and panel.

**Status and doctor.**

1. `status_provider_status_is_fetched_fresh` — two calls with a changed
   daemon verdict return two different answers; nothing is cached.
2. `status_provider_status_unavailable_when_daemon_down`.
3. `doctor_reports_each_provider_readiness`.
4. `health_no_longer_reports_a_provider`.

**Enable.**

1. `enable_writes_a_provider` — after enable, `vox.md` names a real provider
   and the reply says which.
2. `enable_reports_when_nothing_is_credentialed` — the proposal falls to the
   platform binary, or the reply says no provider could be proposed. No
   silent empty write.

---

## 8. Formal-modelling verdict

**Does not qualify. No Z specification is required before implementation.**

The gate in `CLAUDE.md` asks for a stateful subsystem with three or more modes
and transitions between them, or an invariant that must hold *across*
transitions. After this change, provider resolution has neither:

- **No modes.** It is a pure function of two inputs — the provider name on the
  wire and the credential facts in the daemon's environment — evaluated per
  request. There is no daemon-held provider state between requests: a provider
  object is constructed inside `get` and discarded
  (`providers/__init__.py:118–123`), and each request carries its own provider
  on the wire (`voxd/speech_handlers.py:68–82`).
- **No transitions to guard.** Switching provider is one atomic
  `write_fields` call on the client side (`server_switches.py:236–240`).
  Nothing in the daemon transitions.
- **The invariant is enforced by deletion, not by maintenance.** "What voxd
  runs equals what state declares" holds because the only code that could
  break it is removed. There is no consistency to preserve across steps, which
  is what a model would check.

Compare the subsystems that do qualify: the music playlist has four modes and
a pool invariant across them (`docs/audio-programs.tex`); the notification
system has real state (`docs/vox-notify.tex`). This is a resolution function
and the deletion of a branch inside it.

What would change my answer, stated so the leader can hold me to it: a
readiness *cache* in the daemon with invalidation rules; a daemon-held
provider handle reused across requests; or making the provider switch a
multi-step transaction. None is in this design, and §5 rules the first two
out explicitly.

One adjacent subsystem plausibly does qualify, and it is not this bead: the
cascade write — compute the model default, fetch the roster over the network,
write three fields, update the session — is a multi-step transaction with a
concurrency invariant that `panel/service.py:228–260` already reasons about
informally, with a mid-fetch re-check under a lock. If a future bead in
vox-awm9 formalises that, it has a case. This design does not touch it.

---

## 9. Decisions for the operator

Each is stated as a decision with my recommendation, not an open question.

**D1 — `vox enable` writes a provider into `vox.md`.** §3.8. This is the piece
of the design that changes a user-visible behaviour beyond the bug fix: enable
gains a side effect and a fresh repo arrives with a concrete provider in a
tracked file. **Recommend: yes.** Without it, every freshly enabled repo hits
the F1 refusal on its first chime, which trades a wrong-provider bug for a
no-audio bug. With it, detection happens exactly once, in front of a human,
and the result is visible in `mic:status` and reviewable in git.

**D2 — `TTS_PROVIDER` stops overriding state at run time.** §3.3. It is in
`PROVIDER_KEY_NAMES` (`keys.py:29`) and is loaded into the daemon environment
(`voxd/config.py:182`), so today it silently outranks `vox.md`
(`providers/__init__.py:154–156`). **Recommend: demote it to an input to the
enable/install proposal.** Leaving it as a run-time override would preserve
precisely the defect this bead closes, wearing an environment variable. Anyone
who wants a per-invocation provider already has `vox say --provider` and
`mic:provider`.

**D3 — `TTS_MODEL` has the same defect and is not fixed here.** The provider
constructors read it directly (`providers/openai.py:62`,
`providers/elevenlabs.py:78`), so an environment variable can outrank the
model in state exactly as `TTS_PROVIDER` outranks the provider.
**Recommend: file a sibling bead under vox-awm9 rather than widen this one.**
It is a separate rollback unit, it needs the model cascade's context, and
folding it in doubles the diff of a P1 fix. If the operator prefers one PR,
say so and I will fold it in — it is a small addition once
`providers/credentials.py` exists.

**D4 — `health.provider` is deleted rather than repurposed.** §3.5. It is read
by `doctor.py:261` and typed at `types_health.py:28`. **Recommend: delete.**
The daemon has no provider; keeping the field and filling it with a readiness
summary would overload one name with two meanings, and the readiness answer
already has its own op and its own type.
