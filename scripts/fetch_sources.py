"""Mirror every source object for the given jurisdictions to the local cache.

Phase 1 of the two-account build: the source bucket (account 053277883003) is
reachable only with the read-only .env keys, while embedding needs Bedrock in a
different account. So we first download ALL objects under working/<J>/ — docs
AND the JSON sidecars (Doc_metadata / RAG_rated_answers / Survey_Alerts) — into
data/regions/<J>/source/, so the build phase can run fully offline (no S3).

  set -a; . ./.env; set +a
  .venv/bin/python scripts/fetch_sources.py [jurisdictions_file]

Reads jurisdiction names (one per line) from the file arg, or stdin, else builds
the full discovered list. Resumable: skips objects already present with the same
size.
"""
import sys

from aosphere_core_index.aws.s3_readonly import ReadOnlyS3
from aosphere_core_index.config import settings


def jurisdictions() -> list[str]:
    if len(sys.argv) > 1:
        return [l.strip() for l in open(sys.argv[1]) if l.strip()]
    if not sys.stdin.isatty():
        names = [l.strip() for l in sys.stdin if l.strip()]
        if names:
            return names
    return sorted(ReadOnlyS3().list_common_prefixes(settings.working_prefix + "/"))


def main() -> None:
    s3 = ReadOnlyS3()
    js = jurisdictions()
    print(f"fetching sources for {len(js)} jurisdictions", flush=True)
    for i, name in enumerate(js, 1):
        folder = f"{settings.working_prefix}/{name}/"
        dest_dir = settings.region_source(name)
        dest_dir.mkdir(parents=True, exist_ok=True)
        keys = s3.list_keys(folder)
        got = 0
        for key in keys:
            base = key.rsplit("/", 1)[-1]
            if not base:
                continue  # skip the folder marker
            dest = dest_dir / base
            if dest.exists() and dest.stat().st_size > 0:
                continue
            try:
                dest.write_bytes(s3.get_bytes(key))
                got += 1
            except Exception as e:  # noqa: BLE001
                print(f"  !! {name}: {base}: {type(e).__name__}", flush=True)
        print(f"[{i}/{len(js)}] {name}: {len(keys)} objects ({got} new)", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
