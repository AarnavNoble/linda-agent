# Live perception comparison (prompt v4)

Each row re-extracts all 12 downloaded faxes live and runs the same deterministic decider. **Decision agreement** compares the routing outcome (action + route + duplicate_of) with the reference Opus extraction committed in `cache/`; **field diffs** also count category, source ID and flags. This measures agreement and repeatability, not accuracy vs. human labels.

| Run | Decision agreement | Unsafe (would auto-route a non-auto fax) | Extra reviews | Field diffs | Extraction errors | $ total | $/fax | Vision p50 / max |
|---|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 #1 | 12/12 | 0 | 0 | 1 | 0 | $0.010 | $0.0009 | 2.0s / 3.8s |
| claude-opus-5-5 #1 | 12/12 | 0 | 0 | 0 | 0 | $0.413 | $0.0344 | 4.4s / 9.3s |
| claude-opus-5-5 #2 | 12/12 | 0 | 0 | 0 | 0 | $0.412 | $0.0344 | 4.9s / 9.3s |
| claude-sonnet-5-5 #1 | 12/12 | 0 | 0 | 2 | 0 | $0.206 | $0.0172 | 3.4s / 6.0s |

## Differences

### claude-haiku-5-5 #1

- `FX-210.source_doc_id: None -> 'RF-3001'`

### claude-sonnet-5-5 #1

- `FX-209.category: None -> 'unknown'`
- `FX-209.source_doc_id: None -> 'U-4009'`

