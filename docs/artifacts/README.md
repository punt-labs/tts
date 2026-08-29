# Design Artifacts

Self-contained HTML documents that accompany the ADR record in `../../DESIGN.md`. Each file is a standalone page — open directly in a browser (or via GitHub's raw view + a local download) to read the visual design.

Origin: these were authored as claude.ai Artifacts during design work and captured here so the durable record does not depend on external URLs.

## Contents

| File | Companion to | What it is |
|------|--------------|-----------|
| [`e-plus-voice-architecture.html`](e-plus-voice-architecture.html) | DES-068, DES-071 | Final E+ voice architecture. Two entry modes: Mode A (session-attached via `/vox:talk`) and Mode B (voice-first, `voxd` launches a fresh session same-host by default). All labels use current DES numbering. |
| [`e-plus-adr-revision.html`](e-plus-adr-revision.html) | DES-068 | Two-walls framing (Wall 1: foreground can't span turns; Wall 2: background can't inject) and the D vs E+ side-by-side comparison that motivated DES-068's reconsideration of DES-066. Historical bridge document; the ratified position lives in the DES entries themselves. |
| [`distributed-target-topology.html`](distributed-target-topology.html) | DES-069, DES-071 | Laptop ↔ WAN ↔ dev-box target topology. Shows where `voxd`, the ElevenLabs Conversational AI session, `mcp-proxy`, and `quarryd` sit relative to each other, and what crosses the WAN (text and events, never PCM). |

## Rendering notes

These pages were written for claude.ai's artifact host, which stamps a `data-theme` attribute on the document root for its light/dark toggle. In a plain browser the `data-theme` markers are absent, so the page uses whatever the viewer's OS `prefers-color-scheme` selects — light by default. Layout, typography, and diagrams render identically.

SVG diagrams and CSS are inline. The one external asset is the Google Fonts stylesheet (IBM Plex family) fetched via `<link>`; the CSS declares system-font fallback stacks, so pages remain legible if that fetch fails. No JavaScript. No external images.

## Status

Each artifact is a snapshot of the ADR-time design. If a design decision changes, the corresponding artifact is either updated in a new PR alongside the DES entry that changes, or a new artifact is added with a versioned filename. Do not silently update these files without a DES entry saying why.
