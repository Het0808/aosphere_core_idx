# Data Privacy retrieval misses — gold review (12 hit@20 misses)

The DP misses are **not recall failures** (the right section is retrieved for every case) and are **not** the SD over-nesting problem. Only ~2 are candidate system defects; the rest are the empty-heading fix (already implemented) or gold-quality issues.

## A. Empty mid-level heading — FIXED by the empty-heading embed fix (4)
Gold cites a section heading whose content lives in its children, so it had nothing to embed. The embed fix now borrows child text; these should rank after the next DP re-bundle.

| Query | Juris | Gold |
|---|---|---|
| Compare data processing agreements | South Korea | `E1.1` "Requirements for sharing data" (was 0-content) |
| Contract with a processor | United Kingdom | `E2.2` "When do these restrictions apply?" (0) |
| Publicly available data for direct marketing | United Kingdom | `D1` "Privacy notice" (0) |
| Notify the PCPI | South Korea | `D5.1` "When is notification/consent…" (0) |

## B. Suspected gold error / granularity — system's answer is equal-or-better (5)
The reranker returns a **more specific or more on-topic** clause than the gold cites. Eval-gold weaknesses, not system defects — do **not** optimize for them.

| Query | Juris | Gold | System #1 (arguably better) |
|---|---|---|---|
| Monitor employee browsing | South Korea | `A1.1` "Laws" (generic scope) | `G2.2` "During employment — Monitoring" ✅ exact topic |
| Voice biometric data | Malaysia | `A3.2` "Types of data" (generic) | `C1.5(d)` "Biometric data" ✅ exact topic |
| Consent for email advertising | United Kingdom | `J1.2` "General position" (intro) | `J2.1(a)` "Electronic marketing to private email" |
| Unsolicited SMS marketing | United Kingdom | `J1.2` "General position" (intro) | `J4.x` unsolicited-marketing clauses |
| Contract with a processor | United Kingdom | `E2.1` "Restrictions on transferring data" (= *transfers*) | `E2.7` "Contracting on behalf of a corporate group" |

## C. Gold key absent from the survey (1)
| Query | Juris | Gold | Note |
|---|---|---|---|
| Dietary requirements sensitive? | Belgium | `A2.3` | Belgium Part A2 has only A2.1/A2.2 — `A2.3` doesn't exist. Likely mislabel; the answer is under `C1.5`/`A3.2`. |

## D. Candidate genuine reranker misses — worth investigating (2)
Content-rich, on-topic gold clauses that nonetheless rank >20 — the only ones that may indicate a real ranking gap.

| Query | Juris | Gold | Ranked instead |
|---|---|---|---|
| Tracking technologies (cookies) | United Kingdom | `K3.1` "Requirement for consent" (8931 chars) | `J4.3(b)` behavioural advertising |
| Cookie walls | Germany | `K3.3` "Consent" (6031 chars) | `K4.1` "Privacy notice" |

## Recommendation
- **A (4):** resolved by the embed fix once DP is re-bundled.
- **B (5) + C (1):** hand to the eval-workbook owner — gold-labeling, not system defects.
- **D (2):** investigate reranking after the SD re-bundle — the genuinely actionable retrieval work.
