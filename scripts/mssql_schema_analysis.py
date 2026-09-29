#!/usr/bin/env python3
"""Full schema analysis of a SQL Server database.

Dumps everything needed to design the MongoDB Atlas Search sync:
tables, columns, types, PKs/FKs, row counts, candidate title/description
columns (with samples + length stats), to a single JSON file.

Usage:
    pip install pymssql            # or: uv pip install pymssql
    python scripts/mssql_schema_analysis.py

Connection comes from env vars (already added to .env):
    MSSQL_HOST, MSSQL_PORT, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DATABASE

Output: data/mssql_schema_analysis.json
Read-only: only SELECTs against system catalog views + TOP-3 samples.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import pymssql
except ImportError:
    sys.exit("pymssql not installed. Run: pip install pymssql")

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "mssql_schema_analysis.json"

# Column-name patterns that suggest searchable text / metadata
TITLE_PAT = re.compile(r"(title|name|label|caption|subject|heading)", re.I)
DESC_PAT = re.compile(r"(desc|summary|abstract|body|text|content|comment|note|detail)", re.I)
STRING_TYPES = {"varchar", "nvarchar", "char", "nchar", "text", "ntext", "xml"}


def load_env() -> None:
    """Minimal .env loader so we don't depend on python-dotenv."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


def connect():
    return pymssql.connect(
        server=os.environ["MSSQL_HOST"],
        port=int(os.environ.get("MSSQL_PORT", "1433")),
        user=os.environ["MSSQL_USER"],
        password=os.environ["MSSQL_PASSWORD"],
        database=os.environ.get("MSSQL_DATABASE", "master"),
        timeout=60,
        login_timeout=30,
        as_dict=True,
    )


def q(cur, sql: str, params=None) -> list[dict]:
    cur.execute(sql, params or ())
    return cur.fetchall()


def main() -> None:
    load_env()
    conn = connect()
    cur = conn.cursor()

    result: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server": os.environ["MSSQL_HOST"],
        "database": os.environ.get("MSSQL_DATABASE"),
    }

    result["version"] = q(cur, "SELECT @@VERSION AS v")[0]["v"]
    result["databases"] = [r["name"] for r in q(
        cur, "SELECT name FROM sys.databases WHERE database_id > 4 ORDER BY name")]

    # --- Tables + row counts (fast, via partition stats) ---
    tables = q(cur, """
        SELECT s.name AS schema_name, t.name AS table_name, t.object_id,
               SUM(CASE WHEN p.index_id IN (0,1) THEN p.row_count ELSE 0 END) AS row_count,
               SUM(p.reserved_page_count) * 8 AS reserved_kb
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.dm_db_partition_stats p ON p.object_id = t.object_id
        GROUP BY s.name, t.name, t.object_id
        ORDER BY s.name, t.name
    """)

    # --- Columns ---
    columns = q(cur, """
        SELECT s.name AS schema_name, t.name AS table_name, c.name AS column_name,
               ty.name AS data_type, c.max_length, c.is_nullable, c.is_identity,
               c.column_id
        FROM sys.columns c
        JOIN sys.tables t ON t.object_id = c.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        ORDER BY s.name, t.name, c.column_id
    """)

    # --- Primary keys ---
    pks = q(cur, """
        SELECT s.name AS schema_name, t.name AS table_name, c.name AS column_name,
               ic.key_ordinal
        FROM sys.key_constraints kc
        JOIN sys.tables t ON t.object_id = kc.parent_object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.index_columns ic ON ic.object_id = kc.parent_object_id
             AND ic.index_id = kc.unique_index_id
        JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE kc.type = 'PK'
        ORDER BY s.name, t.name, ic.key_ordinal
    """)

    # --- Foreign keys ---
    fks = q(cur, """
        SELECT fk.name AS fk_name,
               ps.name AS parent_schema, pt.name AS parent_table, pc.name AS parent_column,
               rs.name AS ref_schema, rt.name AS ref_table, rc.name AS ref_column
        FROM sys.foreign_key_columns fkc
        JOIN sys.foreign_keys fk ON fk.object_id = fkc.constraint_object_id
        JOIN sys.tables pt ON pt.object_id = fkc.parent_object_id
        JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
        JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
        JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id
        JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
        JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
        ORDER BY ps.name, pt.name
    """)

    # --- Views (may already join entities usefully) ---
    views = q(cur, """
        SELECT s.name AS schema_name, v.name AS view_name
        FROM sys.views v JOIN sys.schemas s ON s.schema_id = v.schema_id
        ORDER BY s.name, v.name
    """)

    # --- Change tracking / rowversion availability (for future incremental sync) ---
    rowversion_cols = [c for c in columns if c["data_type"] in ("timestamp", "rowversion")]
    audit_cols = [c for c in columns
                  if re.search(r"(modified|updated|created).*(date|at|on|time)|(date|time).*(modified|updated|created)",
                               c["column_name"], re.I)]

    # --- Candidate title/description columns + samples ---
    col_by_table: dict[tuple, list] = {}
    for c in columns:
        col_by_table.setdefault((c["schema_name"], c["table_name"]), []).append(c)

    rows_by_table = {(t["schema_name"], t["table_name"]): t["row_count"] for t in tables}
    candidates = []
    for (schema, table), cols in col_by_table.items():
        if rows_by_table.get((schema, table), 0) == 0:
            continue
        for c in cols:
            if c["data_type"] not in STRING_TYPES:
                continue
            kind = ("title" if TITLE_PAT.search(c["column_name"])
                    else "description" if DESC_PAT.search(c["column_name"]) else None)
            if not kind:
                continue
            cand = {"schema": schema, "table": table, "column": c["column_name"],
                    "kind": kind, "data_type": c["data_type"], "max_length": c["max_length"]}
            try:
                stats = q(cur, f"""
                    SELECT COUNT([{c['column_name']}]) AS non_null,
                           AVG(CAST(LEN([{c['column_name']}]) AS FLOAT)) AS avg_len
                    FROM [{schema}].[{table}]
                """)[0]
                cand.update(non_null=stats["non_null"],
                            avg_len=round(stats["avg_len"] or 0, 1))
                samples = q(cur, f"""
                    SELECT TOP 3 [{c['column_name']}] AS v FROM [{schema}].[{table}]
                    WHERE [{c['column_name']}] IS NOT NULL AND LEN([{c['column_name']}]) > 0
                """)
                cand["samples"] = [str(s["v"])[:200] for s in samples]
            except Exception as e:  # permissions / weird types — keep going
                cand["sample_error"] = str(e)[:200]
            candidates.append(cand)

    result.update(
        table_count=len(tables),
        tables=tables,
        columns=columns,
        primary_keys=pks,
        foreign_keys=fks,
        views=views,
        rowversion_columns=rowversion_cols,
        audit_timestamp_columns=audit_cols,
        search_candidates=candidates,
    )

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str))
    print(f"Wrote {OUT_PATH}")
    print(f"  tables: {len(tables)}, columns: {len(columns)}, "
          f"FKs: {len(fks)}, search candidates: {len(candidates)}")


if __name__ == "__main__":
    main()
