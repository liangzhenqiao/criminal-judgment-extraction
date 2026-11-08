# Known limitations

- The reproduction sample is stratified by year; it is not a quality benchmark.
- Of 4,529,346 first-instance criminal judgments dated 2014–2021, 2,935,200
  entered extraction; the logs do not record where the rest left.
- The 2014–2021 consolidation ran in an earlier working copy; its scripts live
  there and are not part of this repository.
- Case numbers are near-unique, not unique (`case_id_collision`).
- The corpus texts are published as a separate figshare item, not here; this
  repository stays code and sample only.
- Refetching a text needs a free platform account and a session cookie, and a
  document the court has withdrawn cannot be fetched back (it stays in the
  published text item, in the state it had when the texts were retrieved).
