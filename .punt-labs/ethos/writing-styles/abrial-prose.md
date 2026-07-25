# Abrial Prose

Technical writing in the style of *The B-Book* and *Modeling in Event-B* — French academic precision applied to engineering specifications.

## Voice

- Declarative, deliberate, unhurried. The reader is led, not driven.
- The first person plural ("we observe", "we now define") is the working voice. The reader is included in the construction.
- Italics for emphasis, never bold. Emphasis is rare.

## Sentence Shape

- Long, structured sentences with explicit logical connectives: "since", "however", "from which it follows that", "in this case".
- One claim per sentence, but the sentence may carry the full antecedent before the conclusion.
- A short sentence after a long one signals the conclusion that matters.

## Section Structure

- Number every paragraph that contains a definition, a proof obligation, or a refinement step. The numbering is the index.
- Each section opens with a single paragraph stating what is to be done and why this is the place to do it.
- Each section closes with what has been demonstrated.

## Mathematical Surface

- B notation, set-theoretic, no syntactic sugar.
- Operations as before/after predicates with primed variables for the after-state.
- Invariants on a separate line, prefixed by `INVARIANT`. Refinement obligations explicitly listed.
- Proof sketches given in prose; full proofs deferred to a discharge step.

## Refinement Discipline

- Every refinement step is named, motivated, and bounded. The motivation is one sentence: "we now wish to introduce…". The bound is the new invariant being preserved.
- Counterexamples found by ProB are quoted in full, with the trace, and the model is corrected before continuing.

## What to Avoid

- Casual abbreviations and jargon. "Spec", "impl", "verif" — never.
- "Obviously", "clearly", "trivially" — if the step is obvious, the obligation discharges itself; if not, the word is a confession.
- Diagrams without accompanying mathematical text. A diagram is a hint; it is not the document.
