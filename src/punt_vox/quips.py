"""Centralized quip registry for hook speech phrases.

Each pool is a tuple of strings — immutable, deterministic ordering for
testing.  ``random.choice()`` picks from them at runtime.

Pools are grouped by hook event.  Keeping all text here simplifies
future localization (swap this module) and theming (load from config).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Stop hook — decision-block reason strings shown in Claude's UI
# ---------------------------------------------------------------------------

STOP_PHRASES: tuple[str, ...] = (
    "\u266a Speaking my thoughts...",
    "\u266a Putting my thoughts into words...",
    "\u266a Summing it up aloud...",
    "\u266a Saying my piece...",
    "\u266a Voicing my closing remarks...",
    "\u266a Letting you hear how it went...",
    "\u266a Telling you what I did...",
)

# ---------------------------------------------------------------------------
# Notification — permission and idle prompt phrases
# ---------------------------------------------------------------------------

PERMISSION_PHRASES: tuple[str, ...] = (
    "Needs your approval.",
    "Quick approval needed.",
    "Need a green light here.",
    "Got a question for you.",
    "Your call on this one.",
    "Mind taking a look?",
    "Waiting on your go-ahead.",
)

IDLE_PHRASES: tuple[str, ...] = (
    "Waiting for your input.",
    "Ready when you are.",
    "Over to you.",
    "Standing by.",
    "Your turn.",
    "What do you think?",
    "Need your thoughts on this.",
)

# ---------------------------------------------------------------------------
# PreCompact — playful 'be right back' before context compaction
# ---------------------------------------------------------------------------

PRE_COMPACT_PHRASES: tuple[str, ...] = (
    "Grabbing a snack, be right back.",
    "Quick bathroom break, one sec.",
    "Stretching my legs for a moment.",
    "Hold that thought \u2014 reorganizing my notes.",
    "Tidying up my desk, back in a flash.",
    "Refilling my coffee, don't go anywhere.",
    "Let me gather my thoughts real quick.",
)

# ---------------------------------------------------------------------------
# UserPromptSubmit — acknowledgment in continuous mode
# ---------------------------------------------------------------------------

ACKNOWLEDGE_PHRASES: tuple[str, ...] = (
    "On it.",
    "Consider it done.",
    "Right away.",
    "Let me take a look.",
    "Got it, working on that now.",
    "Sure thing.",
    "I'm on the case.",
    "Let me dig into that.",
    "Already on it.",
    "You got it.",
)

# ---------------------------------------------------------------------------
# SubagentStart — announcing subagent spawn
# ---------------------------------------------------------------------------

SUBAGENT_START_PHRASES: tuple[str, ...] = (
    "Let me get some help on this one.",
    "Bringing in a specialist.",
    "Calling for backup.",
    "Spinning up a helper.",
    "Let me hand this piece off.",
    "Delegating to a focused agent.",
    "Getting a second pair of eyes.",
    "Farming this out real quick.",
    "Let me pull in some expertise.",
    "Dispatching a helper for this.",
)

# ---------------------------------------------------------------------------
# SubagentStop — announcing subagent completion
# ---------------------------------------------------------------------------

SUBAGENT_STOP_PHRASES: tuple[str, ...] = (
    "Got some good input here.",
    "Back with results.",
    "That helped.",
    "Okay, got what I needed.",
    "The helper came through.",
    "Got the answer I was looking for.",
    "Good, that piece is done.",
    "Nice, that filled in the gaps.",
    "Alright, I can work with this.",
    "Intel received, moving on.",
)

# ---------------------------------------------------------------------------
# vox call — instant turn acknowledgment, spoken the moment a human turn
# closes and before the claude round trip starts (vox-36xc's latency
# mitigation: per-turn subprocess spawn measured 13-25s median, so the human
# gets an immediate "I heard you" instead of dead silence for the whole
# wait). Deliberately its own pool, not a reuse of ACKNOWLEDGE_PHRASES above
# -- that pool's register reads as acknowledging a *coding task* ("I'm on
# the case.", "Already on it.", "Let me dig into that."), which is subtly
# wrong for acknowledging what someone just said in a live conversation.
# These read as conversational turn-taking fillers instead.
# ---------------------------------------------------------------------------

CALL_ACK_PHRASES: tuple[str, ...] = (
    "Mm-hmm.",
    "Got it.",
    "Okay.",
    "Right.",
    "One moment.",
    "Let me think about that.",
    "Give me a second.",
    "Okay, thinking.",
    "Sure, one sec.",
    "Alright.",
)

# ---------------------------------------------------------------------------
# SessionEnd — farewell speech
# ---------------------------------------------------------------------------

FAREWELL_PHRASES: tuple[str, ...] = (
    "See you next time!",
    "Until next session.",
    "Signing off.",
    "Catch you later.",
    "That's a wrap for now.",
    "Good working with you.",
    "Until we meet again.",
    "Take it easy.",
    "Over and out.",
    "Logging off \u2014 talk soon.",
)
