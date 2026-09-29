# Label audit of the 33 retrieval misses

Audited against clause texts in `data/products/*/​*/artifacts/*.content.json` (expected clause vs top-5 retrieved), 2026-07-05.

## Verdict summary

| Verdict | n | Meaning |
|---|---|---|
| LABEL_CORRECT | 13 | True retrieval miss — gold clause is right, retrieval failed |
| LABEL_INCOMPLETE | 8 | Retrieved clause(s) also validly answer — gold set should be expanded |
| LABEL_WRONG | 6 | Gold clause doesn't answer; a retrieved clause is the better answer |
| KEY_NOT_FOUND | 4 | Gold key missing from section index — extraction bug, not label or retriever |
| NOT_RETRIEVAL_CASE | 2 | Dynamic/comparative question; shouldn't be in the scorable pool |

**Effective hit@20 after label correction: ~536/555 ≈ 96.6%** (crediting INCOMPLETE/WRONG where retrieval found a valid answer). Excluding the 2 non-retrieval cases and 4 extraction-bug cases, true retriever error is ~13/549 (~2.4%).

## Per-case verdicts

### Data Privacy + v2 (12 cases)

| Question (trunc) | Expected | Verdict | Notes |
|---|---|---|---|
| Contract with processor in UK…(1) | UK E2.1 | LABEL_INCOMPLETE | Retrieved F6.2(b), F2.5 validly answer; true gold for contract half (E1.1) neither expected nor retrieved |
| Contract with processor in UK…(2) | UK E2.2 | LABEL_INCOMPLETE | Same; E2.2 text is EMPTY in corpus (title-only) |
| Compare DPA reqs UK vs South Korea | SK E1.1 | NOT_RETRIEVAL_CASE | Two-jurisdiction comparison; single-jurisdiction retrieval can't satisfy |
| Monitor employee browsing history (SK) | SK A1.1 | LABEL_WRONG | Retrieved G2.2 explicitly covers browsing history under PIPA — better answer |
| Consent for email advertising (England) | UK J1.2 | LABEL_INCOMPLETE | J2.1(a)/(b)/(d), J6.2 answer directly and more specifically |
| Unsolicited SMS marketing (UK) | UK J1.2 | LABEL_INCOMPLETE | J6.2 (PECR electronic mail incl. SMS) validly answers |
| Publicly available data for DM (UK) | UK D1 | LABEL_INCOMPLETE | J3.5 answers part 1; J3.1 answers part 2; D1 covers only half |
| Tracking technologies in Britain | UK K3.1 | LABEL_INCOMPLETE | J4.3(b), J3.8, K1.2 validly answer for specific tracking tech |
| "Cooker walls" in Germany | DE K3.3 | LABEL_WRONG | Retrieved K4.1 answer-block explicitly addresses cookie walls; query typo |
| Notify "PCPI" of children's data (SK) | SK D5.1 | LABEL_CORRECT | True miss; query typo (PIPC); gold excerpt is drafting-instructions only |
| Dietary requirements sensitive (BE) | BE A2.3 | KEY_NOT_FOUND | A2.3 absent; retrieved A3.2 is almost certainly the intended gold (mis-keyed) |
| Voice biometric data (MY) | MY A3.2 | LABEL_CORRECT | True two-hop miss; A3.2 excerpt truncated before biometric definition |

### Shareholding Disclosure (21 cases)

| Question (trunc) | Expected | Verdict | Notes |
|---|---|---|---|
| Changes in issuer's share capital (JM) | JM A3.3.3 | LABEL_CORRECT | Vocab gap: "share capital" vs clause's "denominator" |
| Change in nature of holding (IN) | IN A3.1.1 | LABEL_WRONG | Retrieved A3.2.2 (non-threshold changes) is the answer — matches NZ gold pattern |
| Passive changes disclosable (ES) | ES A4.3.3 | LABEL_CORRECT | More direct clause A2.5(d) "Passive disclosure" neither gold nor retrieved |
| Netting rules (AU) | AU A3.3.2(c) et al | LABEL_CORRECT | A3.2.2 cites ASIC no-netting rule; A3.3.2(c) exists in docx but not index |
| Long puts in scope (ES) | ES A7.4.2, A7.5.2 | LABEL_CORRECT | Vocab gap: "long put" vs "buyer of the put option" |
| Disclosure if legal owner… (AU) | AU A5.21 | LABEL_CORRECT | Retrieved A3.6.3 is issuer location — wrong entity |
| Can I net my holdings (JM) | JM A3.3.2(c) | KEY_NOT_FOUND | Clause exists in docx; extractor drops parenthetical sub-keys |
| Are TRS disclosable (AU) | AU A1.7, A3.6.1 | LABEL_CORRECT | Acronym gap; A3.5.6 "Treasury shares" retrieved = TRS lexical false match |
| Exemptions from disclosure (AU) | AU A5 | LABEL_INCOMPLETE | Retrieved A1.3 "Exemptions" (s609) also valid — add to gold |
| Collateral provider obligation (ES) | ES A4.3.2(a)(vii) | KEY_NOT_FOUND | Nested lettered clause dropped by extractor; verified in source |
| Parent disclose subsidiaries (JM) | JM A4 | LABEL_CORRECT | Gold is EMPTY parent node; content in children A4.2/A4.3 — repoint gold |
| Indian bank limits non-voting shares | IN B1.1, B1.3.1, B1.4(b) | KEY_NOT_FOUND | Substantive B1.4(b) missing from index |
| Foreign ownership limits (ES) | ES B1 | LABEL_INCOMPLETE | Retrieved B2/B2.1 detail the FDI mechanisms; B1 cross-refers to them |
| Sensitive industry disclosure Spanish banks | ES B6 | LABEL_WRONG | B6 = "Sources of Law"; retrieved B7/B7.1 "How to make a Disclosure" is correct |
| Disclosure form defence holdings (ES) | ES B3 | LABEL_WRONG | Gold is empty parent; real answer B7(c)/B7(d)(i)(4) — neither gold nor retrieved |
| Online disclosure to CNMV (ES) | ES B6 | LABEL_WRONG | B6 wrong again; retrieved B7 covers CNMV filing mechanics |
| 2% in a bidder (ES) | ES C1, C3 | LABEL_CORRECT | Best static answer; A4.1.2 plausible partial |
| List issuers in takeover period (AU) | AU C1 | NOT_RETRIEVAL_CASE | Live data; move to behavioural bucket |
| UBO rules (AU) | AU D5 | LABEL_CORRECT | Acronym gap: "UBO" vs spelled-out title |
| Aggregate group holdings short positions (ES) | ES E1 | LABEL_CORRECT | Borderline: E1 redirects to EU SSR memo; corpus can't fully answer |
| Change in nature New Zealand | NZ A3.2.2 | LABEL_CORRECT | True miss; terse query |

## Systematic findings

1. **Spain Part-B golds are systematically mislabeled** — three "how do I disclose" questions point at B6 "Sources of Law"; the retriever correctly found B7 "How to make a Disclosure". These misses are retrieval wins.
2. **Extractor drops parenthetical/nested sub-keys** — `A3.3.2(c)`, `A4.3.2(a)(vii)`, `B1.4(b)` exist in source docx but not the section index. Fix extraction, and re-key BE A2.3→A3.2.
3. **Empty parent nodes used as gold** (JM A4, ES B3, UK E2.2) — content lives in children; gold should point at populated child clauses, and/or parents should aggregate child text at embed time.
4. **Two-part questions with single-clause golds** (UK processor-contract, publicly-available-data) — gold sets cover half the question.
5. **True misses cluster on jargon**: TRS, UBO, long puts, netting, "share capital" vs "denominator" — a synonym/query-expansion layer would recover most of the 13 genuine misses.
6. Query typos in the eval set itself: "cooker walls", "PCPI" (→PIPC).
