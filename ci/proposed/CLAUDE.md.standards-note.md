# Proposed CLAUDE.md addition (item 1.6)

Agents cannot edit `CLAUDE.md`. A person adds the bullet below to the end of the
"What this repository is" section, directly after the bullet on general practice, and then
deletes this file.

---

- **Standards map.** `docs/standards-map.yaml` tags every feature to general practice and is
  served at `GET /api/v1/standards`. The no-quoting rule applies there in full: paraphrase, cite a
  section number, and never paste wording, tables or figures. Each entry's prose (title, notes and
  AI-standard name) is capped at 200 characters, and a test in
  `apps/api/tests/test_standards.py` rejects longer entries and unknown values. When you build a
  feature, add or update its entry under its milestone (`P<n>-<slug>`).
