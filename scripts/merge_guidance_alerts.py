"""Copy legacy UK's curated guidance (answers_by_clause) + alerts (alerts_by_id) into the
Claude content.json, so reembed builds the SAME guidance/alert rows -> a true full-vs-full.
Guidance is clause-keyed; where Claude lacks the exact key (roman-vs-alpha sub-letters,
split parents) attach it to Claude's nearest existing ancestor clause. Alerts are standalone
(not clause-keyed) so they transfer 1:1."""
import json, sys

CL = sys.argv[1]        # claude content.json to enrich (in place)
LEG = sys.argv[2]       # legacy content.json to copy guidance/alerts from

leg = json.load(open(LEG))
cl = json.load(open(CL))
ckeys = {s["key"].split("#")[0] for s in cl["sections"]}

def nearest(k):
    if k in ckeys:
        return k
    if "(" in k:                       # C1.5(a) -> C1.5
        k = k[:k.index("(")]
        if k in ckeys:
            return k
    while "." in k[1:]:                 # C1.5 -> C1 -> ...
        k = k[:k.rfind(".")]
        if k in ckeys:
            return k
    return k if k in ckeys else None

leg_g = leg.get("answers_by_clause", {})
merged, attached, dropped = {}, 0, 0
for key, lst in leg_g.items():
    t = nearest(key)
    if t:
        merged.setdefault(t, []).extend(lst); attached += 1
    else:
        dropped += 1
cl["answers_by_clause"] = merged
cl["guidance_by_id"] = leg.get("guidance_by_id", {})
cl["alerts_by_id"] = leg.get("alerts_by_id", {})
cl["alerts"] = leg.get("alerts", [])
cl["alerts_by_clause"] = leg.get("alerts_by_clause", {})
json.dump(cl, open(CL, "w"), ensure_ascii=False)
print(f"guidance clause-keys: {attached} attached ({dropped} dropped) -> {len(merged)} claude clauses; "
      f"alerts: {len(cl['alerts_by_id'])}")
