"""Text normalization for natural speech synthesis.

Converts programmer-facing strings (snake_case, camelCase, abbreviations)
to natural spoken English before passing text to TTS providers.  Applied
once per segment, before ``SynthesisRequest`` construction.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Abbreviation dictionary — whole-word expansions
# ---------------------------------------------------------------------------

_ABBREVIATIONS: dict[str, str] = {
    # Programmer terms
    "stderr": "standard error",
    "stdout": "standard output",
    "stdin": "standard input",
    "eof": "E O F",
    "env": "environment",
    "dir": "directory",
    "config": "configuration",
    "args": "arguments",
    "params": "parameters",
    "auth": "authentication",
    "repo": "repository",
    "src": "source",
    "tmp": "temporary",
    "deps": "dependencies",
    "impl": "implementation",
    "init": "initialize",
    "ctx": "context",
    # Internet slang
    "lol": "laughing out loud",
    "rofl": "rolling on the floor laughing",
    "lmao": "laughing my ass off",
    "lmfao": "laughing my fucking ass off",
    "smh": "shaking my head",
    "tbh": "to be honest",
    "imo": "in my opinion",
    "imho": "in my humble opinion",
    "fwiw": "for what it's worth",
    "afaik": "as far as I know",
    "brb": "be right back",
    "ttyl": "talk to you later",
    "ftw": "for the win",
    "omg": "oh my god",
    "wtf": "what the fuck",
}

# ---------------------------------------------------------------------------
# Pronounceable acronyms — TTS engines handle these as words
# ---------------------------------------------------------------------------

_PRONOUNCEABLE_ACRONYMS: frozenset[str] = frozenset(
    {
        # 2-letter English words — TTS pronounces these correctly
        "OK",
        "IT",
        "OR",
        "ON",
        "IN",
        "NO",
        "IF",
        "IS",
        "AT",
        "UP",
        "TO",
        "GO",
        "DO",
        "SO",
        "BE",
        "BY",
        "MY",
        "AM",
        "AN",
        "AS",
        "US",
        "HE",
        "ME",
        "WE",
        # 3-5 letter English words common in ALL_CAPS identifiers
        "MAX",
        "MIN",
        "ADD",
        "GET",
        "SET",
        "PUT",
        "RUN",
        "LOG",
        "MAP",
        "POP",
        "END",
        "ALL",
        "ANY",
        "HAS",
        "LEN",
        "KEY",
        "NEW",
        "OLD",
        "OUT",
        "ERR",
        "MSG",
        "ACK",
        "NET",
        "RAN",
        "RAW",
        "ROW",
        "TAB",
        "TAG",
        "USE",
        "BIT",
        "BUS",
        "CAN",
        "HIT",
        "MIX",
        "PIN",
        "TOP",
        "VIA",
        "WAR",
        "BUG",
        "FIX",
        "TRY",
        "FOR",
        "NOT",
        "VAL",
        "RED",
        "DIM",
        "HOME",
        "TIME",
        "TASK",
        "CALL",
        "FIND",
        "LOOP",
        "STEP",
        "CASE",
        "HELP",
        "HASH",
        "LOCK",
        "FREE",
        "FULL",
        "HALF",
        "LONG",
        "MAIN",
        "MAKE",
        "MARK",
        "MASK",
        "KEEP",
        "KILL",
        "FROM",
        "INTO",
        "HAVE",
        "BEEN",
        "ELSE",
        "JUST",
        "MUCH",
        "ONCE",
        "ONLY",
        "OVER",
        "SOME",
        "TAKE",
        "THAN",
        "THAT",
        "THEM",
        "THEN",
        "THIS",
        "VERY",
        "WHAT",
        "WHEN",
        "WILL",
        "WITH",
        "WORK",
        "ZERO",
        "BACK",
        "BASE",
        "BIND",
        "BODY",
        "BOTH",
        "CHAR",
        "DEEP",
        "EACH",
        "EDGE",
        "EVEN",
        "EXEC",
        "FLAT",
        "FORK",
        "GATE",
        "GOAL",
        "GONE",
        "GOOD",
        "GROW",
        "HARD",
        "HEAD",
        "HERE",
        "HIGH",
        "HOOK",
        "IDLE",
        "ITEM",
        "JOBS",
        "JUMP",
        "LAZY",
        "LEAF",
        "LEFT",
        "LESS",
        "LIKE",
        "LIVE",
        "MISS",
        "MORE",
        "MUST",
        "NEED",
        "NONE",
        "NORM",
        "PEEK",
        "PICK",
        "PLAY",
        "PREV",
        "PULL",
        "PUSH",
        "RATE",
        "REST",
        "RICH",
        "ROLL",
        "ROOT",
        "RULE",
        "SAFE",
        "SCAN",
        "SEED",
        "SELF",
        "SHOW",
        "SHUT",
        "SIGN",
        "SLIM",
        "SLOW",
        "SNAP",
        "SOFT",
        "SPAN",
        "SPEC",
        "SPIN",
        "SWAP",
        "TAIL",
        "TEMP",
        "TERM",
        "TICK",
        "TIER",
        "TINY",
        "TOOL",
        "TRIM",
        "TURN",
        "UNIT",
        "USED",
        "USER",
        "WALK",
        "WALL",
        "WANT",
        "WARM",
        "WEAK",
        "WIDE",
        "WILD",
        "WRAP",
        "RETRY",
        "COUNT",
        "VALUE",
        "DATA",
        "TYPE",
        "NAME",
        "FILE",
        "PATH",
        "SIZE",
        "MODE",
        "CODE",
        "OPEN",
        "READ",
        "SEND",
        "WAIT",
        "DONE",
        "FAIL",
        "PASS",
        "STOP",
        "TEST",
        "TRUE",
        "NULL",
        "VOID",
        "INIT",
        "EXIT",
        "LAST",
        "NEXT",
        "SORT",
        "CHECK",
        "CLOSE",
        "START",
        "STATE",
        "ERROR",
        "EVENT",
        "QUERY",
        "RESET",
        "WRITE",
        "TOKEN",
        "DEBUG",
        "LEVEL",
        "QUEUE",
        "INDEX",
        "INPUT",
        "LIMIT",
        "MATCH",
        "MERGE",
        "MOUNT",
        "PARSE",
        "PATCH",
        "PAUSE",
        "POINT",
        "POWER",
        "PRINT",
        "PROXY",
        "RANGE",
        "REPLY",
        "ROUND",
        "ROUTE",
        "SCENE",
        "SCOPE",
        "SCORE",
        "SETUP",
        "SHARE",
        "SHELL",
        "SHIFT",
        "SHORT",
        "SLEEP",
        "SPACE",
        "SPLIT",
        "STACK",
        "STAGE",
        "STORE",
        "STRIP",
        "SUPER",
        "TABLE",
        "THEME",
        "TIMER",
        "TITLE",
        "TOTAL",
        "TOUCH",
        "TRACE",
        "TRACK",
        "TRAIN",
        "UPPER",
        "VALID",
        "WATCH",
        "WHERE",
        "WHILE",
        "WORLD",
        "YIELD",
        "ABORT",
        "APPLY",
        "BATCH",
        "BLOCK",
        "BREAK",
        "BUILD",
        "CACHE",
        "CATCH",
        "CHAIN",
        "CHUNK",
        "CLAIM",
        "CLASS",
        "CLEAN",
        "CLEAR",
        "CLONE",
        "COLOR",
        "CONST",
        "COVER",
        "CRASH",
        "CYCLE",
        "DEFER",
        "DEPTH",
        "DRAFT",
        "DRAIN",
        "DRIVE",
        "EMPTY",
        "ENTER",
        "EXTRA",
        "FAULT",
        "FETCH",
        "FIELD",
        "FINAL",
        "FIRST",
        "FIXED",
        "FLAGS",
        "FLASH",
        "FLOAT",
        "FLUSH",
        "FOCUS",
        "FORCE",
        "FRAME",
        "FRESH",
        "FRONT",
        "GIVEN",
        "GRACE",
        "GRAPH",
        "GREEN",
        "GROUP",
        "GUARD",
        "GUESS",
        "HAPPY",
        "HEART",
        "HEAVY",
        "HUMAN",
        "IMAGE",
        "INNER",
        "LARGE",
        "LATER",
        "LAYER",
        "LEASE",
        "LIGHT",
        "LOCAL",
        "LOWER",
        "MAGIC",
        "MINOR",
        "MODEL",
        "MOVED",
        "MULTI",
        "MUTEX",
        "NEVER",
        "NEWER",
        "NOISE",
        "NOTED",
        "OFFER",
        "ORDER",
        "OTHER",
        "OUTER",
        "OWNER",
        "PANEL",
        "PHASE",
        "PLAIN",
        "PRIME",
        "PROTO",
        "QUICK",
        "QUIET",
        "QUOTA",
        "RAISE",
        "READY",
        "REALM",
        "REFER",
        "RIGHT",
        "SINCE",
        "SMALL",
        "SOLID",
        "SOUTH",
        "SPENT",
        "STILL",
        "STOCK",
        "STUFF",
        "THEIR",
        "THIRD",
        "THROW",
        "TODAY",
        "UNION",
        "UNTIL",
        "USING",
        "WHITE",
        "SKIP",
        "DROP",
        "COPY",
        "MOVE",
        "SAVE",
        "LOAD",
        "SYNC",
        "TEXT",
        "WORD",
        "LINE",
        "PAGE",
        "NODE",
        "TREE",
        "LIST",
        "POOL",
        "PIPE",
        "PORT",
        "HOST",
        "LINK",
        "INFO",
        "WARN",
        # Acronyms TTS engines pronounce correctly as words
        "JSON",
        "SQL",
        "NATS",
        "AWS",
        "YAML",
        "CUDA",
        "RAM",
        "ROM",
        "LAN",
        "WAN",
        "SIM",
        "GIF",
        "BASH",
        "RUST",
        "JAVA",
        "PERL",
        "RUBY",
    }
)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# camelCase / PascalCase boundary: insert space before an uppercase letter
# that follows a lowercase letter or digit, or before a run of uppercase
# letters followed by a lowercase letter (e.g. "HTMLParser" → "HTML Parser").
_CAMEL_BOUNDARY = re.compile(
    r"(?<=[a-z])(?=[A-Z])"  # fooBar → foo Bar
    r"|(?<=[A-Z])(?=[A-Z][a-z])"  # HTMLParser → HTML Parser
    r"|(?<=[a-zA-Z])(?=[0-9])"  # foo2bar → foo 2bar
    r"|(?<=[0-9])(?=[a-zA-Z])"  # foo2bar → foo2 bar (combined: foo 2 bar)
)

# A "programmer token" contains underscores or camelCase/digit boundaries.
# We process these; plain English words pass through unchanged.
_HAS_UNDERSCORE = re.compile(r"_")
_HAS_CAMEL = re.compile(r"[a-z][A-Z]|[A-Z]{2,}[a-z]|[a-zA-Z][0-9]|[0-9][a-zA-Z]")

# Tokens that look like file paths — leave these alone.
_FILE_PATH_RE = re.compile(r"^[~/.]?/")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


VIBE_TAG_RE = re.compile(r"\[([a-z]+)\]")


def strip_vibe_tags(text: str) -> str:
    """Remove ElevenLabs-style expressive tags from *text*.

    Strips bracketed tokens **at any position** that contain a single
    lowercase-alpha word -- e.g. ``[serious]``, ``[warm]``, ``[sighs]``.

    Preserves brackets that don't match this narrow pattern:
    ``[Figure 1]`` (has space/uppercase), ``[1]`` (digits),
    ``[citation needed]`` (two words), ``[slow breath]`` (two words),
    ``[IMPORTANT]`` (all caps).

    Collapses any extra whitespace left behind after removal.

    .. note::

       ``resolve.strip_expressive_tags`` strips only *leading* tags and
       is used during vibe application (before synthesis).  This function
       strips tags at *any* position and is used by providers that do not
       support expressive tags at all (say, espeak) to sanitise the full
       text before passing it to a subprocess.
    """
    result = VIBE_TAG_RE.sub("", text)
    # Collapse runs of whitespace left behind by removal.
    result = re.sub(r"  +", " ", result).strip()
    # Guard: if stripping removed all content, return original text
    # unchanged so providers don't receive an empty string.
    if not result:
        return text
    return result


def normalize_for_speech(text: str) -> str:
    """Normalize programmer strings in *text* to natural spoken English.

    Transforms applied per token (whitespace-delimited):

    1. **File paths** (start with ``/``, ``~/``, ``./``) — skipped.
    2. **snake_case** — underscores replaced with spaces, parts processed
       individually for abbreviation expansion.
    3. **camelCase / PascalCase** — split on case boundaries.
    4. **Known abbreviations** — expanded (case-insensitive whole-word).

    Idempotent: running twice produces the same result.
    """
    words = text.split()
    result: list[str] = []
    for word in words:
        normalized = _normalize_token(word)
        if normalized:
            result.append(normalized)
    return " ".join(result)


def _normalize_token(token: str) -> str:
    """Normalize a single whitespace-delimited token."""
    # Strip non-speech symbols; only prosody punctuation survives in suffix
    core, suffix = _strip_punctuation(token)

    if not core:
        # Token was only non-speech symbols — drop it
        return suffix if suffix else ""

    # File paths: keep core as-is but still strip non-speech wrapping
    if _FILE_PATH_RE.match(core):
        return core + suffix

    # snake_case: split on underscores, process each part
    if _HAS_UNDERSCORE.search(core):
        return _expand_parts(core.split("_"), suffix)

    # camelCase / PascalCase: split on case boundaries
    if _HAS_CAMEL.search(core):
        return _expand_parts(_CAMEL_BOUNDARY.sub(" ", core).split(), suffix)

    # Standalone abbreviation
    expanded = _expand_abbreviation(core)
    if expanded != core:
        return expanded + suffix

    # ALL_CAPS standalone: space out acronyms for TTS spelling
    if core.isupper() and len(core) > 1:
        spaced = _space_acronym(core)
        if spaced != core:
            return spaced + suffix

    # Return reconstructed token (punctuation may have been stripped)
    return core + suffix


def _expand_parts(parts: list[str], suffix: str) -> str:
    """Expand each identifier part, rejoin with spaces, and append *suffix*.

    The shared tail of the snake_case and camelCase branches: both split an
    identifier into parts and differ only in how they split, so the
    expand-and-rejoin step lives here once.
    """
    return " ".join(_expand_part(p) for p in parts if p) + suffix


def _expand_part(part: str) -> str:
    """Expand a single sub-token (after snake/camel splitting).

    Also applies camelCase splitting so that mixed identifiers like
    ``get_fileName`` fully normalize to ``get file name``.
    """
    expanded = _expand_abbreviation(part)
    if expanded != part:
        return expanded
    # camelCase within a snake_case part (e.g. "fileName" from "get_fileName")
    if _HAS_CAMEL.search(part):
        split = _CAMEL_BOUNDARY.sub(" ", part)
        sub_parts = split.split()
        return " ".join(_expand_part(p) for p in sub_parts)
    # ALL_CAPS: space out acronyms so TTS spells them letter-by-letter,
    # unless the word is pronounceable (real word or known acronym).
    if part.isupper() and len(part) > 1:
        return _space_acronym(part)
    return part.lower() if part[0].isupper() and not part.isupper() else part


def _space_acronym(part: str) -> str:
    """Space out ALL_CAPS tokens so TTS engines spell them letter-by-letter.

    Returns the input unchanged if it contains digits, is in the
    pronounceable-acronyms allowlist, is a single character, or is
    longer than 5 characters.
    """
    if len(part) < 2 or len(part) > 5:
        return part
    if not part.isalpha() or not part.isupper():
        return part
    if part in _PRONOUNCEABLE_ACRONYMS:
        return part
    return " ".join(part)


def _expand_abbreviation(word: str) -> str:
    """Look up a word in the abbreviation dictionary (case-insensitive)."""
    return _ABBREVIATIONS.get(word.lower(), word)


def _strip_punctuation(token: str) -> tuple[str, str]:
    """Strip leading/trailing punctuation, returning (core, suffix).

    Leading punctuation is always discarded; leading ``~/._`` are kept in
    core (file path / identifier prefixes).
    Only prosody-affecting punctuation (``.``, ``,``, ``?``, ``!``, ``:``,
    ``;``, em-dash, en-dash) is preserved in suffix -- all other trailing
    symbols (parentheses, brackets, slashes, etc.) are discarded since
    TTS engines either mispronounce them or produce artifacts.
    Trailing underscores are discarded -- they are separators, not speech.
    """
    start = 0
    end = len(token)
    while start < end and not token[start].isalnum() and token[start] not in "~/._":
        start += 1
    # Peel off trailing punctuation (not underscore, not alnum) → suffix
    suffix_start = end
    while suffix_start > start:
        ch = token[suffix_start - 1]
        if ch.isalnum() or ch == "_":
            break
        suffix_start -= 1
    # Discard trailing underscores (separators, not speech)
    core_end = suffix_start
    while core_end > start and token[core_end - 1] == "_":
        core_end -= 1
    # Only keep prosody-affecting suffix characters; drop the rest
    raw_suffix = token[suffix_start:]
    suffix = "".join(filter(_PROSODY_PUNCTUATION.__contains__, raw_suffix))
    # Leading punctuation is never speech — always discarded above
    return token[start:core_end], suffix


# Punctuation that affects TTS prosody (pauses, intonation) — keep these.
# Everything else (parens, brackets, slashes, etc.) is dropped.
_PROSODY_PUNCTUATION = frozenset(".,?!:;\u2014\u2013")
