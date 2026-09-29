#!/usr/bin/env python3
"""Nav-only republish: rebuild the given docs' MinerU viewers (picks up the
nav-order fix in assets/viewer_template.html) and OVERWRITE them at their
existing S3 keys. Does NOT touch manifest.json — content, keys and stats are
unchanged, so the 184 already-correct MinerU cards stay intact (avoids the
manifest race where a partial push would drop the other -mineru entries)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import push_hybrid_s3 as PH
import extract_to_s3 as E


def main(stems):
    import boto3
    from botocore.config import Config
    cfg = Config(connect_timeout=10, read_timeout=90, retries={"max_attempts": 4})
    s3 = boto3.Session(profile_name=PH.PROFILE, region_name=PH.REGION).client("s3", config=cfg)
    # PH.PREFIX never existed — this said `PH.PREFIX` and would AttributeError on its first
    # document. gallery_prefix() is the resolver the publisher and the reader both use, so
    # this now overwrites the keys the gallery is actually serving rather than a guess at
    # them. (Found while extracting that resolver into lib_gallery; fixed here because
    # writing to the wrong prefix is the exact failure that refactor exists to prevent.)
    prefix = PH.gallery_prefix(s3)
    print(f"republishing viewers under s3://{PH.BUCKET}/{prefix}/")
    jobs = {j["doc_id"]: j for j in E.discover(PH.SOURCE, None, None)}

    ok = fail = 0
    for stem in stems:
        s3dir = Path("out/hybrid") / stem / "03_stage3_final"
        j = jobs.get(stem)
        if not s3dir.exists() or not j:
            print(f"  ⚠ skip {stem} (no stage3 / not in corpus)"); fail += 1; continue
        product, region, pdf = j["product"], j["region"], Path(j["pdf"])
        title = f"{product} — {region} (MinerU)"
        html = PH.build_viewer(s3dir, pdf, title, "2-pass hybrid: pdf2mdtree + MinerU tables")
        assert "renderChildren" in html, f"{stem}: nav fix missing in built viewer!"
        gslug = PH.gslug(product, region, stem)
        vkey = f"{prefix}/{product}/{region} (MinerU)/{gslug}-viewer.html"
        s3.put_object(Bucket=PH.BUCKET, Key=vkey, Body=html.encode("utf-8"),
                      ContentType="text/html; charset=utf-8")
        print(f"  ✓ {region:38s} {len(html)//1024}KB")
        ok += 1
    print(f"\nnav-only republish done: {ok} ok, {fail} skipped. Manifest untouched.")


if __name__ == "__main__":
    main(sys.argv[1:])
