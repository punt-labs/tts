---
description: "Spoken summary of what just happened"
allowed-tools: ["mcp__plugin_vox_mic__unmute"]
---

# /recap command

Speak a brief summary of the last response.

## Usage

`/recap`

## Implementation

1. Look at your most recent response (before this command) — the one the user wants summarized.
2. Extract the 2-3 most important points: what changed, what was done, any key findings.
3. Write the summary as clear, concise spoken text (30 seconds when spoken, roughly 60-80 words).
4. Call the `unmute` MCP tool with:
   - `text`: your summary
   - `ephemeral`: `true`
5. Show the summary text in the conversation as well (voice supplements text, never replaces it).

Keep the summary factual. No filler words. No "Here's a summary of..." preamble — just the content.
