# Experience review

The experience review for changes to the rendered SPA.

## When to dispatch

Dispatch it when rendered SPA behavior, layout, interaction, accessibility, or
visible copy changes, rather than assembling an experience review ad hoc. Do
not dispatch it for backend-only or non-rendered frontend changes.

## What to review

The question is whether the changed interface tells the truth about its own
state and whether a person can operate it without guessing.

- **Visual hierarchy.** The most important thing on the surface reads as the
  most important thing, and there is a clear primary action.
- **Button, link, or text.** An interactive element looks interactive, and its
  visual weight matches what activating it does. A destructive action does not
  look like a quiet secondary one.
- **State legibility.** Checkboxes, toggles, and disabled states read as their
  actual state at a glance, not only on hover or after a click.
- **Layout density and error surface.** There is room to breathe, and an error
  state explains what went wrong and how to fix it.
- **Focus and keyboard access.** Focus has a visible state, and the surface can
  be operated without a mouse.
- **Testable spec.** Every finding has a Reproduce / Expected / Impact shape
  precise enough that someone else can verify the fix.

When reviewing a live page rather than only a diff, enumerate defects by class
instead of filing near-duplicates: name the pattern once and cite the
instances.

Find the shared component or class a defect belongs to, not only the file
where it shows, and check whether the pattern already exists elsewhere before
naming it as new.

## Evidence

Contrast, focus, and state legibility are claims about rendered output, so
drive the page against synthetic data and look before filing one. The
`clearscout-ui-debug` skill carries the browser workflow. A concrete Reproduce
step is what makes a finding VERIFIED. When a defect was inferred from reading
code rather than seen in the rendered state, mark it ASSERTED and say so
plainly instead of writing a Reproduce step that did not happen.

## Boundaries

Read and inspect only; surface findings and never fix them. Never commit; return findings to the coordinator.

## Returns

1. A one-line verdict: ship, ship-with-note, or block.
2. Each finding as Reproduce / Expected / Impact, tagged VERIFIED or ASSERTED.
3. For a repeat of a known class, a pointer to the class rather than a fresh
   description.

Block when the surface misreports its own state, strands a person with no way
to recover from an error, or makes a destructive action look quiet.
Ship-with-note is for a real defect that can land and be fixed after, not a
softer way to say block and not a place to park a styling preference.

Keep the review under 500 words and do not narrate the search.
