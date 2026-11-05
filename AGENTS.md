# Project Instructions

- Communicate with the maintainer in Chinese.
- Preserve the pinned harness, benchmark source, native grader, and raw evaluation records.
- Run focused tests for changed behavior and the full suite before publishing a release.
- Keep credentials, personal documents, and local runtime state out of Git.

## Resume Maintenance

The maintainer keeps a private resume linked to this project. When a verified feature,
evaluation result, or release materially changes its project entry:

1. Read `.runtime/resume/content.json` if available. It identifies the local document,
   approved project text, and rendering workflow. Preserve the document's unrelated content.
2. Update claims from implementation and retained results. Describe the business task,
   personal engineering work, and a small number of meaningful, correctly scoped outcomes.
3. Consult technical-resume examples when restructuring the entry. Keep operational logs,
   model access details, dependency pins, and unsupported performance claims in project docs.
4. Use the private `.runtime/resume/update.py` helper to prepare a candidate. Keep backups,
   candidates, contact details, and reviews under the ignored runtime directory.
5. Render with Word or LibreOffice, inspect the page layout, and validate DOCX structure.
6. After editing, start a new subagent with `fork_context=false` to independently inspect
   the actual document, rendered pages, and supporting evidence. Address factual and
   presentation issues before replacing the user's DOCX and paired PDF.
7. Update the local evidence/review record. Keep the delivered resume concise and current.

Public clones without the private configuration can skip the local resume workflow.
