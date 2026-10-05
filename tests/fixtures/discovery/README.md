# Discovery test fixtures (fictional)

All data here is fictional and exists only for offline, deterministic tests.

- `knowledge_map_phoenix.json`: a `lofgren.knowledge-map/1` exported from the V1 pipeline run on the fictional
  Phoenix sample texts in `tests/helpers.py`.
- `knowledge_map_empty.json`: a `lofgren.knowledge-map/1` from a V1 certification scenario that produced no
  claims (only unknowns), used for the `INSUFFICIENT_EVIDENCE` path.
- `prior_art_corpus.json`: four fictional prior-art records and the coverage the fixture provider declares.
