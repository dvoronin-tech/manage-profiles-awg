# Local Markdown issue tracker

Issues and specs for this project live as Markdown files under `.scratch/`.

## Conventions

- Use one directory per feature at `.scratch/<feature-slug>/`.
- Save the spec at `.scratch/<feature-slug>/spec.md`.
- Save each implementation ticket in its own file at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`. Start numbering at `01`.
- Record the triage label on a `Status:` line near the top of each ticket. Use the strings in [Triage labels](triage-labels.md).
- Append comments and conversation history under `## Comments` at the end of the ticket.

## Publishing and fetching

When a skill asks you to publish a spec or ticket, create its file at the appropriate path above and create its directory if needed.

When a skill asks you to fetch a ticket, read the referenced file. For a ticket number, look for the matching file in the relevant feature's `issues/` directory.
