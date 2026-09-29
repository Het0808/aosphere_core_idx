# Eval-case suite report

- Total cases parsed: **722**
- Retrieval-scorable (has expected citation + jurisdiction indexed): **555**
- **Citation hit@40: 545/555 (98%)**
- Behavioural / no-citation cases (need agent eval): **152**
- Cases whose jurisdiction isn't in the index: **15**
- Pipeline: reranker=`claude-bedrock`, query_expansion=`True`

## Ranking quality (exact clause) — hit@N

_**hit@N** = share of queries whose exact expected clause is in the top N reranked results (higher is better; it's a yes/no per query). **MRR** = mean reciprocal rank: the average of 1 ⁄ (rank of the first correct clause) — 1.0 if it's always #1, 0.5 if #2, 0.33 if #3, 0 if not found. It rewards ranking the answer high; MRR ≈ 0.62 means the right clause typically sits around rank 1–2._

| Scope | n | hit@1 | hit@3 | hit@5 | hit@10 | hit@20 | hit@40 | MRR |
|---|---|---|---|---|---|---|---|---|
| Overall | 555 | 48% | 69% | 79% | 90% | 97% | 98% | 0.62 |
| Data Privacy | 299 | 43% | 66% | 76% | 87% | 96% | 98% | 0.58 |
| Shareholding Disclosure | 256 | 54% | 73% | 83% | 93% | 97% | 98% | 0.66 |

## Topic-area hit (expected clause's section in top-N)

_Credits returning the right section family (e.g. any H1.* for expected H1.7) — closer to 'is the relevant area surfaced'._

| Scope | n | sec@3 | sec@5 | sec@10 |
|---|---|---|---|---|
| Overall | 555 | 85% | 90% | 95% |
| Data Privacy | 299 | 89% | 92% | 95% |
| Shareholding Disclosure | 256 | 80% | 89% | 94% |

## By product

| Product | scorable | hit@40 |
|---|---|---|
| Data Privacy | 299 | 98% |
| Shareholding Disclosure | 256 | 98% |

## By sheet

| Sheet | total | scorable | hit@40 | behavioural | not-indexed |
|---|---|---|---|---|---|
| DP:Children | 20 | 20 | 95% | 0 | 0 |
| DP:Consent | 29 | 29 | 100% | 0 | 0 |
| DP:Cookies | 14 | 14 | 93% | 0 | 0 |
| DP:DPO | 13 | 13 | 100% | 0 | 0 |
| DP:Data breach | 90 | 90 | 100% | 0 | 0 |
| DP:Data sharing | 22 | 22 | 91% | 0 | 0 |
| DP:Data subject rights | 29 | 29 | 100% | 0 | 0 |
| DP:Direct marketing | 20 | 20 | 100% | 0 | 0 |
| DP:Employee monitoring | 17 | 17 | 100% | 0 | 0 |
| DP:Privacy notice | 20 | 20 | 100% | 0 | 0 |
| DP:Security | 16 | 16 | 100% | 0 | 0 |
| SD:A Aggregation | 9 | 9 | 100% | 0 | 0 |
| SD:A Capacity | 13 | 12 | 100% | 1 | 0 |
| SD:A Disclosure | 14 | 14 | 100% | 0 | 0 |
| SD:A Securities | 35 | 35 | 97% | 0 | 0 |
| SD:A Trigger events | 16 | 16 | 100% | 0 | 0 |
| SD:AppendicesSupplementscross-refs | 27 | 12 | 100% | 0 | 15 |
| SD:B Disclosure | 12 | 12 | 83% | 0 | 0 |
| SD:B Restrictions | 18 | 17 | 100% | 1 | 0 |
| SD:C Takeovers | 24 | 23 | 100% | 1 | 0 |
| SD:D Issuer request | 24 | 24 | 100% | 0 | 0 |
| SD:E Disclosure | 22 | 19 | 100% | 3 | 0 |
| SD:E Obligations | 51 | 43 | 98% | 8 | 0 |
| SD:Examples | 20 | 20 | 100% | 0 | 0 |
| v2:Negatives | 17 | 0 | — | 17 | 0 |
| v2:Out-of-scope | 11 | 0 | — | 11 | 0 |
| v2:Redirect to tools | 71 | 4 | 100% | 67 | 0 |
| v2:Self-referencing | 10 | 0 | — | 10 | 0 |
| v2:TL retrieval | 20 | 0 | — | 20 | 0 |
| v2:Topics to not answer | 12 | 0 | — | 12 | 0 |
| v2:Two-hop Qs | 6 | 5 | 60% | 1 | 0 |

## Behavioural cases — not retrieval-scored (152)

_These have **no expected clause**, so citation hit@k can't grade them — success is the agent taking the right **action** (redirect / refuse / advisory / negative answer), recorded in the source `action` column. They are excluded from the hit@k tables above and need a separate **agent-behaviour eval** (does the agent act correctly?)._

| Category | n | expected behaviour |
|---|---|---|
| v2:Redirect to tools | 67 | multi-jurisdiction / cross-tool asks → redirect to the right aosphere tool (e.g. Compare), with advisory text |
| v2:TL retrieval | 20 | top-level / terse queries (e.g. 'dpo uk') → surface the topic |
| v2:Negatives | 17 | correct reply is a negative / advisory ('no, that isn't sufficient'), not a clause citation |
| v2:Topics to not answer | 12 | should REFUSE — historical / political / off-domain questions |
| v2:Out-of-scope | 11 | outside the survey's scope → decline or advise, don't fabricate a cite |
| v2:Self-referencing | 10 | about the assistant/tool itself ('what are you?') → handled specially |
| SD:E Obligations | 8 | answer expected but no clause tagged in the source |
| SD:E Disclosure | 3 | answer expected but no clause tagged in the source |
| SD:A Capacity | 1 | answer expected but no clause tagged in the source |
| SD:B Restrictions | 1 | answer expected but no clause tagged in the source |
| SD:C Takeovers | 1 | answer expected but no clause tagged in the source |
| v2:Two-hop Qs | 1 | answer expected but no clause tagged in the source |

## Miss analysis

_Each miss bucketed by the fix it needs. Only the **retrieval** bucket is a system change; **agent-solvable** misses already surface the right section (the agent reads + traverses) or need multi-hop reasoning; **gold** misses are benchmark-label errors for review._

| Bucket | n | how to close |
|---|---|---|
| Retrieval-fixable | 4 | broaden LLM query-expansion (jargon) + typo→retrieval |
| Agent-solvable | 4 | right section already found (agent traverses); decomposition for two-hop |
| Gold / benchmark error | 2 | eval-owner review of the expected citation |

## Retrieval misses (10)

- [DP:Data sharing] **Do you need a contract with a processor in the UK and does it make a d** — expected `United Kingdom:E2.1` · right-section@7; got `E1.1(b), E1.1(b)(ii), F2.1(b), F5, B1.2`
- [DP:Data sharing] **Do you need a contract with a processor in the UK and does it make a d** — expected `United Kingdom:E2.2` · right-section@7; got `E1.1(b), E1.1(b)(ii), F2.1(b), F5, B1.2`
- [DP:Cookies] **Can we use cooker walls in Germany?** — expected `Germany:K3.3` · section not found; got `K4.1, K1.1, K, G2.1.2, G2`
- [DP:Children] **Do we need to notify the PCPI in South Korea of processing children's ** — expected `South Korea:D5.1` · section not found; got `C1.7, C3.9, C3.9.1, D1.7, ALERT:19160`
- [SD:A Securities] **Do I need to make a shareholding disclosure in Australia if the legal ** — expected `Australia:A5.21` · section not found; got `A3.6.3, A1.1, B2.3.1, A1.2, A3.1.1`
- [SD:B Disclosure] **Does Spain have a disclosure form for holdings in defence** — expected `Spain:B3` · section not found; got `A8.3, B2.1.1, A2.1, B7, A2.3.1`
- [SD:B Disclosure] **Can I disclose online to the CNMV for holdings in investment firms (Sp** — expected `Spain:B6` · section not found; got `A8.4, E4.1.1, B7, A4.1.2, C7.1.1`
- [SD:E Obligations] **How to aggregate group holdings for short position reporting in Spain** — expected `Spain:E1` · section not found; got `A2.6, A5.3, A6.1.2, A5.4, A5.2`
- [v2:Two-hop Qs] **Are dietary requirement sensitive data in Belgium?** — expected `Belgium:A2.3` · right-section@19; got `A3.2, C1.5(a), C1.4, D4.2, C1.5`
- [v2:Two-hop Qs] **Is someone's voice biometric data in Malaysia?** — expected `Malaysia:A3.2` · right-section@37; got `C1.5(d), G4.2(d), E1, A1.1, A1`