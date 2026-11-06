# Perturbation probe (fail-closed check)

Source run: `results/sample/results.json`; extraction mode: `vision`. Only faxes the agent auto-routed are perturbed (a damaged copy of an already-reviewed fax is trivially safe).

**live vision on the damaged PDF: 12 variants, 0 auto-routed; decider on edited extractions: 20 variants, 0 auto-routed** (target 0 auto-routed).

| Fax | Damage | Path | Engine | Decision | First reason |
|---|---|---|---|---|---|
| FX-201 | blank_page_1 | pipeline/vision | vision | **needs_review** | Incomplete or unreadable: p1: page is illegible; page markers [2] have gaps (expected [1, 2]) |
| FX-201 | blank_page_1 | simulated/blank_unreadable | cached+edited | **needs_review** | Incomplete or unreadable: p1: page is illegible; page markers [2] have gaps (expected [1, 2]) |
| FX-201 | blank_page_1 | simulated/blank_as_cover | cached+edited | **needs_review** | Incomplete or unreadable: page markers [2] have gaps (expected [1, 2]) |
| FX-201 | blank_page_2 | pipeline/vision | vision | **needs_review** | Incomplete or unreadable: p2: page is illegible; page markers [1] have gaps (expected [1, 2]) |
| FX-201 | blank_page_2 | simulated/blank_unreadable | cached+edited | **needs_review** | Incomplete or unreadable: p2: page is illegible; page markers [1] have gaps (expected [1, 2]) |
| FX-201 | blank_page_2 | simulated/blank_as_cover | cached+edited | **needs_review** | Incomplete or unreadable: page markers [1] have gaps (expected [1, 2]) |
| FX-201 | drop_page_1 | pipeline/vision | vision | **needs_review** | Incomplete or unreadable: extraction covers 1 pages but portal lists 2; page markers [2] have gaps (expected [1, 2]) |
| FX-201 | drop_page_1 | simulated/drop | cached+edited | **needs_review** | Incomplete or unreadable: extraction covers 1 pages but portal lists 2; page markers [2] have gaps (expected [1, 2]) |
| FX-201 | drop_page_2 | pipeline/vision | vision | **needs_review** | Incomplete or unreadable: extraction covers 1 pages but portal lists 2; page markers [1] have gaps (expected [1, 2]); p1: refers to content  |
| FX-201 | drop_page_2 | simulated/drop | cached+edited | **needs_review** | Incomplete or unreadable: extraction covers 1 pages but portal lists 2; page markers [1] have gaps (expected [1, 2]); p1: refers to content  |
| FX-203 | blank_page_1 | pipeline/vision | vision | **needs_review** | Incomplete or unreadable: p1: page is illegible |
| FX-203 | blank_page_1 | simulated/blank_unreadable | cached+edited | **needs_review** | Incomplete or unreadable: p1: page is illegible |
| FX-203 | blank_page_1 | simulated/blank_as_cover | cached+edited | **needs_review** | Document type could not be established from the scanned content |
| FX-204 | blank_page_1 | pipeline/vision | vision | **needs_review** | Incomplete or unreadable: p1: page is illegible |
| FX-204 | blank_page_1 | simulated/blank_unreadable | cached+edited | **needs_review** | Incomplete or unreadable: p1: page is illegible |
| FX-204 | blank_page_1 | simulated/blank_as_cover | cached+edited | **needs_review** | Document type could not be established from the scanned content |
| FX-205 | blank_page_1 | pipeline/vision | vision | **needs_review** | Incomplete or unreadable: p1: page is illegible |
| FX-205 | blank_page_1 | simulated/blank_unreadable | cached+edited | **needs_review** | Incomplete or unreadable: p1: page is illegible |
| FX-205 | blank_page_1 | simulated/blank_as_cover | cached+edited | **needs_review** | Document type could not be established from the scanned content |
| FX-206 | blank_page_1 | pipeline/vision | vision | **needs_review** | Incomplete or unreadable: p1: page is illegible; page markers [2] have gaps (expected [1, 2]) |
| FX-206 | blank_page_1 | simulated/blank_unreadable | cached+edited | **needs_review** | Incomplete or unreadable: p1: page is illegible; page markers [2] have gaps (expected [1, 2]) |
| FX-206 | blank_page_1 | simulated/blank_as_cover | cached+edited | **needs_review** | Incomplete or unreadable: page markers [2] have gaps (expected [1, 2]) |
| FX-206 | blank_page_2 | pipeline/vision | vision | **needs_review** | Incomplete or unreadable: p2: page is illegible; page markers [1] have gaps (expected [1, 2]) |
| FX-206 | blank_page_2 | simulated/blank_unreadable | cached+edited | **needs_review** | Incomplete or unreadable: p2: page is illegible; page markers [1] have gaps (expected [1, 2]) |
| FX-206 | blank_page_2 | simulated/blank_as_cover | cached+edited | **needs_review** | Incomplete or unreadable: page markers [1] have gaps (expected [1, 2]) |
| FX-206 | drop_page_1 | pipeline/vision | vision | **needs_review** | Incomplete or unreadable: extraction covers 1 pages but portal lists 2; page markers [2] have gaps (expected [1, 2]) |
| FX-206 | drop_page_1 | simulated/drop | cached+edited | **needs_review** | Incomplete or unreadable: extraction covers 1 pages but portal lists 2; page markers [2] have gaps (expected [1, 2]) |
| FX-206 | drop_page_2 | pipeline/vision | vision | **needs_review** | Incomplete or unreadable: extraction covers 1 pages but portal lists 2; page markers [1] have gaps (expected [1, 2]); p1: refers to page 2 ( |
| FX-206 | drop_page_2 | simulated/drop | cached+edited | **needs_review** | Incomplete or unreadable: extraction covers 1 pages but portal lists 2; page markers [1] have gaps (expected [1, 2]); p1: refers to page 2 ( |
| FX-207 | blank_page_1 | pipeline/vision | vision | **needs_review** | Incomplete or unreadable: p1: page is illegible |
| FX-207 | blank_page_1 | simulated/blank_unreadable | cached+edited | **needs_review** | Incomplete or unreadable: p1: page is illegible |
| FX-207 | blank_page_1 | simulated/blank_as_cover | cached+edited | **needs_review** | Document type could not be established from the scanned content |

How to read this:

- `pipeline/replay`: the damaged PDF hashes differently, so replay has no extraction and the agent routes to Manual Review with the reason. This is the real fail-closed path (no evidence, no auto-route).
- `simulated/*`: the original cached extraction edited as a correct vision pass would report the damage. `blank_as_cover` is adversarial (model calls the white page a clean cover sheet); the decider still catches it through page-marker gaps or the absence of any content page.
- Not shown without an API key: whether the live model reports the damage at all. Run `ANTHROPIC_API_KEY=... uv run python -m eval.perturb --mode vision` to measure that (~$0.03/variant).
