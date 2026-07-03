# Planning & research artifacts

Working documents that inform the code but are not part of it: investigation
reports, design notes, decision records.

## Naming convention

```
YYYY-MM-DD-<topic>-<kind>[.private].md
```

- **date** — when the work was done (sorts the folder chronologically).
- **topic** — short kebab-case slug, e.g. `unattributed-gst`.
- **kind** — what the document is: `research` (investigation findings),
  `plan` (implementation plan), `decision` (why we chose X over Y).
- **`.private` suffix** — the file quotes real account data (statement rows,
  amounts, dates). Such files are **gitignored** (`planning/*.private.md`)
  and exist only locally; committed documents may describe findings in
  general terms but must never quote statement contents.

Example: `2026-07-03-unattributed-gst-research.md` (a sanitized research report;
its unsanitized original would have been `...-research.private.md`, local only)
