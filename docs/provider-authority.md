# Provider authority — state decides, voxd obeys or refuses

**Bead:** vox-w3f8 (P1 bug). **Parent epic:** vox-awm9 (provider → model →
voice cascade and state authority). This is the state-authority half.

**Type:** design. No code in this document's change; the write set in §6 is a
recommendation for the implementation mission, not a prescription carried over
from the bead.

**Status:** amended after design review. The review carried one defect and four
rulings, all now in the text: enable proposes **through the daemon**, not from
the client process (§3.8); `TTS_MODEL` folds into this bead rather than a
sibling (§3.9, D3 overruled); the work splits into **three** PRs, not two (§6);
D1, D2, D4, and D5 are approved; D3 was overruled and folded into this bead.
No decision remains open — this document is settled and implementation may
proceed against it.

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

That is the line between `vox enable` asking the daemon for a sensible starting
provider and writing the answer into `vox.md`, and `voxd` guessing on every
request. The first is a recorded, visible, reviewable decision. The second is
the bug.

The proposal must come from the process that holds the credentials — the
daemon — for the same reason the refusal does. A client that proposes from its
own environment writes a provider the daemon cannot run, which is this bug
again with a friendlier face (§3.8).

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

`auto_detect`, `_resolve_choice`, `_has_aws_credentials`, and the `_last_logged`
dedup machinery (`providers/__init__.py:125–188`, `:62–68`) are **deleted
outright**, not renamed. An earlier draft of this design kept them under the
name `propose()`, on the reasoning that proposing a value for state is a
legitimate job. The reasoning holds; keeping this code does not. The proposal
must be answered by the process that owns the credentials — the daemon — and
the daemon already answers it from `ProviderCredentials` (§3.4). A second
environment-probing code path in `providers/__init__.py` would be a second
source of truth for the same question, differing from the first in that it
shells out to the `aws` CLI (`providers/__init__.py:171–188`) where the
readiness object consults botocore directly. One question, one answer, one
place.

Proposal therefore becomes a method on the readiness object —
`ProviderCredentials.preferred()` — and reaches clients only through the
`provider_status` wire op (§3.6). §3.8 uses it at enable time.

`TTS_PROVIDER` (`keys.py:29`, loaded into the daemon environment at
`voxd/config.py:182`) survives as exactly one thing: an input to
`ProviderCredentials.preferred()`, which returns it when it names a provider
that is *ready* and returns the first ready provider in the fixed order
(elevenlabs, openai, polly, platform binary) otherwise. It can no longer
override state at run time. Without that demotion the bug this bead closes
returns wearing an environment variable.

The distinction that makes this safe, stated once because it is the line the
whole design turns on: reading `TTS_PROVIDER` to *answer a question a client
asked, whose answer the client then writes into state* is not a substitution.
Reading it to *decide what to synthesize with, right now, instead of state* is.
The first is visible, recorded, and happens at enable; the second is the defect.

`providers/openai.py:62` and `providers/elevenlabs.py:78, :138` read
`TTS_MODEL` in exactly the second form. §3.9 removes it.

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

The class has three entry points, and every caller gets the same answer from
the same code:

- `require(provider)` — raises `ProviderUnavailableError` when unmet. Called
  by `ProviderRegistry.get`.
- `report(provider)` / `report_all()` — the same verdict without raising.
  Called by the `provider_status` wire op (§3.6).
- `preferred()` — the name a fresh repo should adopt: `TTS_PROVIDER` when it
  names a ready provider, else the first ready provider in the fixed order,
  else `None` when nothing is ready. Called by the same wire op, for §3.8.

Status therefore cannot drift from behaviour, and enable cannot propose a
provider the daemon would refuse: they are one function, called three ways.

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

`mic:status` also gains a flat `model` field. It reports provider, voice,
notify, speak, and the vibe cluster today (`server.py:647–659`) but not the
model, and under this rule status reports what voxd will run with — of which
the model is part (§3.9).

```json
{
  "provider": "polly",
  "model": null,
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

The daemon side is a new wire op, `provider_status`, whose reply carries three
things:

- with a `provider` field on the request, that provider's verdict;
- without one, all five verdicts (what `vox doctor` wants) plus
- `preferred` — `ProviderCredentials.preferred()`, the name a fresh repo should
  adopt, or `null` when nothing on this daemon is ready. This is what §3.8
  uses, and it is why enable does not probe its own environment.

It gets its own module, `voxd/provider_status_handler.py`, because
`voxd/system_handlers.py` already holds three classes and PY-OO-2 caps a module
at three.

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
action="enable"` writes a real provider into `vox.md`** and reports which one
it wrote. Detection survives exactly once, where a human is present, and its
result lands in state where `mic:status` shows it and git records it.

**The daemon chooses it, not the client.** Enable calls the `provider_status`
op (§3.6) and takes its `preferred` field. It does not call a local
`propose_provider()` and it does not read its own environment.

An earlier draft of this section had enable propose from the CLI/MCP process.
That is the same defect §3.5 identifies in `doctor.py:228–244`: the CLI's
environment is not the daemon's. `voxd` runs as a launchd/systemd service with
a stripped environment and loads its credentials from `keys.env`
(`voxd/config.py:155–185`, `keys.py:1–7`), so a shell with
`ELEVENLABS_API_KEY` exported and a daemon with no `keys.env` disagree
completely. Proposing from the client would write a provider the daemon cannot
run — recreating the exact divergence this bead closes, at the one moment a
human is watching and would trust the answer. The daemon is asked because the
daemon is the one that will have to honour it.

Two consequences worth stating:

- **Enable needs voxd running.** If the op cannot be reached, enable writes no
  provider and says so: `vox enable` completes the rest of its work (marker,
  guide, settings) and reports "voxd is not reachable; no provider was
  selected — start the daemon and run `vox provider <name>`". It does not
  guess locally as a consolation.
- **Nothing ready is not an error.** When `preferred` is `null` — no keys, no
  platform binary — enable writes no provider and names the condition. That is
  a true report of an unusable host, which is more useful than a provider that
  will refuse on first use.

`vox desktop install` keeps its own `keys.env` read
(`desktop_install.py:98–148`) rather than calling the op, and that is not an
inconsistency: it runs at install time, when voxd may not exist yet, and it
reads *the daemon's own credential file* rather than its own environment. It
is already asking the right question of the right source
(`desktop_install.py:101–106` says so explicitly). It re-points at
`providers/credentials.py` for the key-variable map (§3.4) and otherwise
stands.

A repo that still reaches synthesis with no provider — a hand-edited file, a
`vox.md` written before enable could reach the daemon — gets the client-side
refusal in §4, F1.

### 3.9 The model: state decides there too

The provider is not the only field an environment variable can override.
`providers/openai.py:62` and `providers/elevenlabs.py:78` both read

```python
self._model = model or os.environ.get("TTS_MODEL") or _DEFAULT_MODEL
```

and `ElevenLabsProvider.model_supports_expressive_tags` repeats the expression
at `providers/elevenlabs.py:138`. `TTS_MODEL` is in `PROVIDER_KEY_NAMES`
(`keys.py:30`) and is therefore loaded into the daemon's environment at
startup (`voxd/config.py:182`). A repo whose `vox.md` says
`model: eleven_flash_v2_5` synthesizes with `eleven_v3` if the daemon's
`keys.env` says so — the identical substitution, one field over, with the
identical consequence: `mic:status` (once it reports the model at all, §3.6)
and the audio disagree.

Shipping the provider fix while leaving this is not defensible, and the
operator has ruled accordingly (§9, D3).

**The rule is the same.** Three changes:

1. `os.environ.get("TTS_MODEL")` is deleted from all three sites. The
   constructors become `model or _DEFAULT_MODEL` — the provider's own
   documented default constant, not an environment probe.
2. `TTS_MODEL` is removed from `PROVIDER_KEY_NAMES` (`keys.py:30`), so
   `vox daemon install` stops snapshotting it into `keys.env`
   (`service/keys_env.py:103`) and the daemon stops loading it. It is state,
   not a credential, and it has no business in the credential file.
3. `session_spec.py` (§3.7) validates the model against the provider before
   the spec goes on the wire, and raises `ModelNotAvailableError` when state
   holds a provider-alien pair (§4, F7).

**Why a default model is not the same as a default provider.** §3.8 refuses
when state declares no provider, but §3.9 falls back to the provider's default
when state declares no model. The asymmetry is deliberate and rests on three
differences:

- A provider is a choice *among five* with different credentials, different
  bills, and different voice rosters — guessing produces the wrong voice from
  the wrong vendor. A model is a choice *within* an already-chosen provider,
  and the wrong guess is a quality or cost difference, not a different vendor.
- The provider default would come from probing the environment. The model
  default is a constant in the provider class
  (`providers/elevenlabs.py:35`, `providers/openai.py:62`), deterministic and
  identical on every host.
- Three of the five providers have no model at all
  (`models.py:136–138`: polly, say, espeak have empty lists), so `model: ""`
  is a permanent, legitimate state. Refusing on an empty model would make
  those three providers unusable.

**The validation the daemon used to do must move.** `ProviderRegistry.get`
today fills the model from config only when the config provider matches the
resolved provider, with the comment "An ElevenLabs model name passed to OpenAI
(or vice-versa) would cause API errors" (`providers/__init__.py:110–117`).
§3.3 deletes that whole config read, so the guard goes with it. It is replaced
in `session_spec.py`, where the state is actually known — and it rejects
rather than drops, because silently dropping `eleven_v3` and using the OpenAI
default is a substitution of exactly the kind this document forbids.

A mismatched pair should be rare: setting the provider rewrites the model in
the same atomic `write_fields` call (`server_switches.py:231, :237`,
`commands/provider.py:75`). It arises from a hand-edited `vox.md` — the same
origin as the unknown-provider case in F4, and it gets the same treatment.

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
- `vox enable`: no provider written, and the reply says so (§3.8).

### F7 — state names a model the provider does not offer

Owner: the client (`session_spec.py`, §3.9). A hand-edited `vox.md` pairing
`provider: openai` with `model: eleven_v3`.

- `mic:unmute`, `vox say`: `{"error": "model 'eleven_v3' is not available for
  provider 'openai' (available: tts-1, tts-1-hd, gpt-4o-mini-tts)"}` — the
  available list from `MODEL_TABLE.available` (`models.py:88–90, :132–135`).
- Rejected, never dropped. Silently substituting the OpenAI default for the
  model state asked for is the same class of substitution as F2's provider
  case.
- `mic:status`: reports the declared `model` verbatim, so the caller sees the
  pair that is wrong rather than a corrected one.
- `mic:model` with no argument still lists correctly — listing needs no
  synthesis and `ModelTool` already resolves against the current provider
  (`server_switches.py:134–140`).

An empty model (`model: ""`, and the permanent state for polly, say, and
espeak) is not this case: it means "no model declared", and the provider's own
default constant applies (§3.9).

---

## 5. What is deliberately not built

- **No fallback in any form.** No warn-and-continue, no try-then-fallback, no
  "best effort", no environment variable or flag that restores the old
  behaviour. The environment probe survives only as
  `ProviderCredentials.preferred()`, answered by the daemon and written into
  state at enable (§3.3, §3.8).
- **No environment override of state, for either field.** `TTS_PROVIDER` is
  demoted to an input to `preferred()`; `TTS_MODEL` is deleted outright
  (§3.9). A run-time environment override is the defect with a different
  trigger.
- **No migration.** `provider: ""` in an existing `vox.md` is handled by the
  refusal in F1 and by enable writing a real value; there is no detector, no
  one-shot upgrade path, no `vox provider --migrate`.
- **No readiness cache in the daemon.** The check is cheap enough to run per
  request, and a cache would need invalidation — which is how a stale verdict,
  the very thing this bead is about, gets reintroduced.
- **No local proposal in any client.** Enable asks the daemon or writes
  nothing (§3.8). A client-side fallback "when voxd is down" would be the
  wrong-process bug wearing a convenience argument.

---

## 6. Recommended write set, in three PRs

The operator has ruled the split: three sequentially dependent PRs, not two.
Twenty-six files plus the `TTS_MODEL` work is past the diff size at which agent
reviewers hold quality, and each of the three reverts coherently on its own.
I accept the boundaries as drawn. I recommend one change to the **order** —
see D5 in §9, ruled and settled.

**The order below is the one I recommend (client half first).** Landing the
daemon gate first leaves `main` mute between PR 1 and PR 2: after the daemon
requires a `provider` on the wire, `vox say` with no `--provider`
(`__main__.py:354–367`), the panel preview (`panel/service.py:329`), and any
repo whose `vox.md` has `provider: ""` (`hooks.py:168`, via `or None`) all
send no provider and are rejected. Filling the clients first is inert — every
surface simply starts sending the provider the daemon was already going to
guess correctly in the common case — and then the gate removes a branch that
has become unreachable.

### PR 1 — the client half: every surface sends the provider from state

Inert on its own: nothing refuses that did not refuse before.

| File | Change | Why |
|------|--------|-----|
| `src/punt_vox/session_spec.py` | **new** — `VoxConfig` → `SynthesisSpec` with a guaranteed provider and a provider-validated model; raises `ProviderNotConfigured` / `ModelNotAvailableError`. | One state-to-spec constructor instead of five, so the next surface cannot reintroduce the bug. Carries the model guard the daemon is about to lose (§3.9). |
| `src/punt_vox/types_errors.py` | add `ProviderNotConfigured`, `ModelNotAvailableError`. | `ValueError` subclasses; the client-owned failures (F1, F7). |
| `src/punt_vox/server.py` | `fill_defaults` delegates to `session_spec`. | The MCP surface. |
| `src/punt_vox/hooks.py` | build the spec through `session_spec`. | Hooks were the path that produced the reported log line. |
| `src/punt_vox/__main__.py` | `say` / `record` fill from state through `session_spec`. | `vox say` ignores `vox.md` today. |
| `src/punt_vox/panel/service.py` | preview fills the provider; drop `or "elevenlabs"` at `:189`. | The preview sends no provider at all today. |
| `src/punt_vox/server_audio_tools.py` | `rec new` fills through `session_spec`. | Already correct; routed through the one constructor so it stays that way. |
| `src/punt_vox/server_switches.py` | drop `or "elevenlabs"` at `:132` and `:321`; label the roster with the provider actually fetched. | Two more silent substitutions. |
| `src/punt_vox/commands/model.py` | drop `or "elevenlabs"`. | The CLI twin of the same substitution. |
| `src/punt_vox/cascade.py`, `client.py`, `client_sync.py` | `voices(provider: str)` / `fetch_roster(..., provider: str)` required. | Propagates the required provider to the roster path. |

### PR 2 — the daemon gate: require it, refuse without credentials

| File | Change | Why |
|------|--------|-----|
| `src/punt_vox/providers/credentials.py` | **new** — `CredentialRequirement` protocol, its five implementations, and `ProviderCredentials` with `require` / `report` / `report_all` / `preferred`. | One home for "can this provider run here", replacing four partial copies. |
| `src/punt_vox/providers/__init__.py` | `get(name: str, *, model)` — drop `None`, the `ConfigStore` read, and the auto-detect branch; call `ProviderCredentials.require` before the factory; **delete** `auto_detect`, `_resolve_choice`, `_has_aws_credentials`, `_last_logged`. | The substitution, the daemon's config read, and the duplicate probe all live here. |
| `src/punt_vox/types_errors.py` | add `ProviderUnavailableError`, `ProviderAuthError`. | The daemon-owned failures (F2, F3). |
| `src/punt_vox/voxd/speech_handlers.py` | require `provider` on the wire; add `_SpeechRequest.reject`; catch the typed provider errors and `VoiceNotFoundError` before the broad guard. | Removes the synthesize-path guess and stops laundering diagnosable errors. |
| `src/punt_vox/voxd/system_handlers.py` | require `provider` on the `voices` op; delete the `auto_detect_provider` import. | Removes the roster-path guess. |
| `src/punt_vox/voxd/synthesis.py` | delete `_PROVIDER_API_KEY_VAR`; take the key-variable map from `providers/credentials.py`. | De-duplicates the credential table. |
| `src/punt_vox/providers/openai.py`, `providers/elevenlabs.py` | delete `os.environ.get("TTS_MODEL")` at `openai.py:62`, `elevenlabs.py:78`, `elevenlabs.py:138`; raise `ProviderAuthError` on a credential rejection. | The model substitution (§3.9) and F3's typed error. |
| `src/punt_vox/keys.py` | derive `PROVIDER_KEY_NAMES` from `providers/credentials.py`; **remove** `TTS_MODEL` (`:30`). | Deletes the fourth copy of the map; the model is state, not a credential. |
| `src/punt_vox/desktop_install.py` | use `providers/credentials.py` for the key-variable map; keep the `keys.env` read (§3.8). | Deletes the third copy. |
| `src/punt_vox/resolve.py` | delete `resolve_voice_and_language` and `_validate_and_infer` only — `apply_vibe`, `split_leading_expressive_tags`, and `strip_expressive_tags` stay (imported by `tests/test_server.py:25`). | No production callers, and it is the same silent-substitution pattern preserved as a template (PY-RF-6, forward integration). |
| `tests/test_resolve.py` | delete the `resolve_voice_and_language` cases; keep the rest. | Follows its subject, function-scoped. |

### PR 3 — observability: the client can see all of it

| File | Change | Why |
|------|--------|-----|
| `src/punt_vox/types_provider.py` | **new** — `ProviderReadiness` (frozen, `from_wire`) and the `reason` `Literal`. | The wire shape both sides need, importable without heavy deps. |
| `src/punt_vox/voxd/provider_status_handler.py` | **new** — the `provider_status` op: one provider, all five, plus `preferred`. | A fourth class will not fit in `system_handlers.py` (PY-OO-2). |
| `src/punt_vox/voxd/handler_registry.py` | register `provider_status`. | Wiring. |
| `src/punt_vox/client.py`, `client_sync.py` | add `provider_status(...)`. | The client half of the new op. |
| `src/punt_vox/server.py` | `status()` merges the `provider_status` block and reports `model`. | The standing question, answerable without synthesizing. |
| `src/punt_vox/voxd/health.py` | delete `payload["provider"]`. | The daemon has no provider to report. |
| `src/punt_vox/types_health.py` | delete the `provider` field and its `from_wire` line. | Follows the payload. |
| `src/punt_vox/enablement.py` | ask the daemon for `preferred` and write it into `vox.md`; report what was written, or why nothing was (§3.8). | Makes the empty state unreachable in the normal path — without the wrong-process bug. |
| `src/punt_vox/doctor.py` | drop `(provider: …)`; add a per-provider readiness section from the new op; drop the caller-env check at `:228–244`. | Every error message points here, so it has to answer. |

### Across all three

| File | Change | Why |
|------|--------|-----|
| `tests/…` | per §7, split by the PR that introduces each behaviour. | |
| `CHANGELOG.md`, `README.md`, `DESIGN.md` | Changed/Fixed entries; the `mic:status` shape; an ADR recording the authority rule, the rejected gate positions, and the enable-asks-the-daemon rule. | Documentation discipline. |

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

1. `enable_writes_the_daemon_s_preferred_provider` — after enable, `vox.md`
   names the provider the `provider_status` op returned as `preferred`, and
   the reply says which.
2. `enable_does_not_read_its_own_environment` — the client process has
   `ELEVENLABS_API_KEY` set and the daemon does not. Enable must write the
   daemon's answer, never `elevenlabs`. This is the regression test for the
   wrong-process defect.
3. `enable_writes_nothing_when_voxd_is_unreachable` — the marker, guide, and
   settings still land; no provider is written; the reply names the condition.
   No local fallback proposal.
4. `enable_writes_nothing_when_no_provider_is_ready` — `preferred` is `null`;
   no silent empty write, and the reply says why.

**Model authority (§3.9).**

1. `tts_model_env_var_does_not_override_state` — the daemon's environment sets
   `TTS_MODEL=eleven_flash_v2_5`, state says `model: eleven_v3`, and the
   ElevenLabs provider is constructed with `eleven_v3`. The model twin of the
   bead's own regression test.
2. `tts_model_env_var_does_not_supply_a_model` — the environment sets
   `TTS_MODEL`, state declares none, and the provider uses its own default
   constant, not the environment value.
3. `tts_model_is_not_in_provider_key_names` — `keys.PROVIDER_KEY_NAMES`
   excludes it, so `vox daemon install` stops snapshotting it into `keys.env`.
4. `provider_alien_model_is_rejected` — `provider: openai` with
   `model: eleven_v3` errors naming the available OpenAI models, and does
   **not** silently fall back to `tts-1`.
5. `empty_model_uses_the_provider_default` — `model: ""` with
   `provider: elevenlabs` synthesizes with `eleven_v3`.
6. `modelless_provider_needs_no_model` — polly, say, and espeak synthesize
   with `model: ""` and no error.
7. `expressive_tag_capability_follows_state` — `model_supports_expressive_tags`
   answers from the spec's model, not from `TTS_MODEL`
   (`providers/elevenlabs.py:138`).
8. `status_reports_the_model` — `mic:status` carries the declared model.

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

## 9. Decisions

D1 through D4 are **ruled**. They are recorded here as decided, with the
ruling, so the implementation missions have one place to read the settled
position. D5 was raised in that round and is now ruled.

**D1 — `vox enable` writes a provider into `vox.md`. APPROVED, with a
correction.** §3.8. Enable gains a side effect: a fresh repo arrives with a
concrete provider in a tracked file. Without it, every freshly enabled repo
hits the F1 refusal on its first chime, trading a wrong-provider bug for a
no-audio bug.

The correction, which the leader caught and which is now the design: the
provider is chosen **by the daemon**, through the `provider_status` op's
`preferred` field, never by a local proposal in the CLI or MCP process. My
first draft had enable probe its own environment — the same wrong-process
mistake §3.5 identifies in `doctor.py:228–244`, and worse for happening at the
one moment a human is watching and would trust the answer. §3.8 now states the
rule, the two consequences (voxd must be reachable; nothing-ready is a report,
not an error), and why `vox desktop install` legitimately differs.

**D2 — `TTS_PROVIDER` stops overriding state at run time. APPROVED.** §3.3. It
is in `PROVIDER_KEY_NAMES` (`keys.py:29`) and is loaded into the daemon
environment (`voxd/config.py:182`), so today it silently outranks `vox.md`
(`providers/__init__.py:154–156`). It is demoted to an input to
`ProviderCredentials.preferred()`, consulted by the daemon when a client asks
what a fresh repo should adopt. Anyone who wants a per-invocation provider has
`vox say --provider` and `mic:provider`.

**D3 — `TTS_MODEL` folds into this bead. OVERRULED; the operator's ruling
stands and the design now carries it.** I recommended a sibling bead. The
operator overruled: shipping a P1 that closes the provider substitution while
knowingly leaving the identical substitution one field over contradicts the
org rule that there is no such thing as an existing issue, and my own words —
that it is a small addition once `providers/credentials.py` exists — were the
deciding fact.

The ruling is right and I withdraw the recommendation. My split reasoning
weighed diff size, which is a PR-boundary concern the operator has now
addressed directly by splitting into three (D5). Diff size was never a reason
to leave a known defect in place; it is a reason to sequence the work. §3.9
specifies the model treatment, F7 gives its client-observable surface, the
test plan gains eight cases, and the write set places the deletions in PR 2
alongside the provider gate they mirror.

**D4 — `health.provider` is deleted rather than repurposed. APPROVED.** §3.5.
It is read by `doctor.py:261` and typed at `types_health.py:28`. The daemon has
no provider; keeping the field and filling it with a readiness summary would
overload one name with two meanings, and the readiness answer already has its
own op and its own type.

**D5 — the order of the three PRs. RULED: client half first.** The split into
three is accepted; the boundaries are not relitigated. The order is
**(1) client half, (2) daemon gate, (3) observability** — a reversal of the
leader's original (1) daemon gate, (2) client half.

The reason is mechanical, not stylistic. PR "daemon gate" makes `provider` a
required wire field. Until the client half lands, three surfaces do not send
one: `vox say` with no `--provider` (`__main__.py:354–367`), the panel preview
(`panel/service.py:329`), and any repo whose `vox.md` holds `provider: ""` —
which is the shipped shape (`DESIGN.md:69`) — through `hooks.py:168`'s
`or None`. Between the two merges, `main` refuses those paths. Every repo that
has not explicitly set a provider loses its chimes, and `vox say` needs a flag
it never needed before.

Reversed, nothing breaks at any point. The client half is inert on its own:
each surface starts sending the provider from state, which the daemon accepts
exactly as it accepts a provider today — the guess simply stops being reached.
The gate PR then deletes a branch that has already become unreachable, and the
refusal it adds is the intended new behaviour arriving in one step.

One dependency moves with the swap: `ProviderNotConfigured` and
`ModelNotAvailableError` are needed by `session_spec.py`, so those two
`types_errors.py` additions belong in the client-half PR;
`ProviderUnavailableError` and `ProviderAuthError` stay with the gate. §6
already reflects the recommended order and this split.

The leader accepted this on review, naming the cause: the three-way split was
made on reviewer-effectiveness grounds, and that split was then allowed to
imply an order. Reviewer load decides where the boundaries fall; the
intermediate states decide which end to start from. They are two questions
answered by the same list, and separating them is the general lesson — whenever
a change is split at all, a broken intermediate state should be an accepted,
recorded cost rather than a discovered one.
