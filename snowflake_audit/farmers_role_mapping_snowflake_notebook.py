"""
FARMERS Role Mapping — Snowflake Notebook
=========================================
Paste each '## ----- CELL N -----' block into a separate Snowflake notebook
cell (Python cell type). The notebook's `session` object is the active
Snowpark session — no separate credentials needed.

Pipeline
--------
1.  Connect to the active Snowpark session
2.  Read role edges + DB/Schema grants from Snowflake into pandas DataFrames
3.  Recursively walk from every FARMERS* master through every descendant
4.  Build a per-(master, db, schema) dict with consolidated permissions and
    a detailed JSON of every contributing child-role path
5.  Write the rows to FARMERS_AUDIT.RBAC.FINAL_MASTER_ACCESS_MAP_PY
    (VARIANT columns for permissions and child_map)
6.  Also drop the JSON file in an internal stage for easy download
7.  Verify by reading the target table back
"""


## ----- CELL 1 — Imports & active Snowpark session -----
import json
from collections import defaultdict
from typing import Dict, Generator, List, Optional, Tuple

import pandas as pd
from snowflake.snowpark.context import get_active_session

session = get_active_session()
print("Snowflake account :", session.get_current_account())
print("Snowflake role    :", session.get_current_role())
print("Snowflake wh      :", session.get_current_warehouse())


## ----- CELL 2 — Configuration -----
SOURCE_DB     = "FARMERS_AUDIT"
SOURCE_SCHEMA = "RBAC"
TARGET_TABLE  = f"{SOURCE_DB}.{SOURCE_SCHEMA}.FINAL_MASTER_ACCESS_MAP_PY"
STAGE_NAME    = f"{SOURCE_DB}.{SOURCE_SCHEMA}.AUDIT_OUTPUT_STAGE"
MASTER_PREFIX = "FARMERS"

# True  -> read live grants from SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
# False -> read from the STG_02 / STG_03 staging tables produced by the
#          companion farmers_role_mapping_pipeline.sql script
USE_ACCOUNT_USAGE = True

# Make sure the destination database / schema / stage exist.
session.sql(f"CREATE DATABASE IF NOT EXISTS {SOURCE_DB}").collect()
session.sql(f"CREATE SCHEMA   IF NOT EXISTS {SOURCE_DB}.{SOURCE_SCHEMA}").collect()
session.sql(f"CREATE STAGE    IF NOT EXISTS {STAGE_NAME}").collect()
print(f"target table : {TARGET_TABLE}")
print(f"output stage : @{STAGE_NAME}")


## ----- CELL 3 — Load role edges into a DataFrame -----
if USE_ACCOUNT_USAGE:
    edges_df = session.sql("""
        SELECT GRANTEE_NAME AS PARENT_ROLE,
               NAME         AS CHILD_ROLE
        FROM   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
        WHERE  GRANTED_ON = 'ROLE'
          AND  PRIVILEGE  = 'USAGE'
          AND  DELETED_ON IS NULL
    """).to_pandas()
else:
    edges_df = (
        session.table(f"{SOURCE_DB}.{SOURCE_SCHEMA}.STG_02_ROLE_EDGES")
               .to_pandas()
    )
edges_df.columns = [c.upper() for c in edges_df.columns]
print(f"edges loaded : {len(edges_df):,} rows")
edges_df.head(20)


## ----- CELL 4 — Load DB/Schema grants into a DataFrame -----
if USE_ACCOUNT_USAGE:
    grants_df = session.sql("""
        SELECT GRANTEE_NAME                 AS ROLE_NAME,
               TABLE_CATALOG                AS DATABASE_NAME,
               COALESCE(TABLE_SCHEMA,'*')   AS SCHEMA_NAME,
               PRIVILEGE                    AS PRIVILEGE
        FROM   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
        WHERE  GRANTED_ON IN ('DATABASE','SCHEMA')
          AND  DELETED_ON IS NULL
          AND  TABLE_CATALOG IS NOT NULL
    """).to_pandas()
else:
    grants_df = (
        session.table(f"{SOURCE_DB}.{SOURCE_SCHEMA}.STG_03_ROLE_DB_GRANTS")
               .to_pandas()
    )
grants_df.columns = [c.upper() for c in grants_df.columns]
print(f"grants loaded : {len(grants_df):,} rows")
grants_df.head(20)


## ----- CELL 5 — Recursive walker -----
def walk_descendants(
    role: str,
    children_index: Dict[str, List[str]],
    path: Optional[List[str]] = None,
    visited: Optional[set] = None,
) -> Generator[Tuple[str, List[str]], None, None]:
    """
    Recursively yield (descendant_role, path_from_master_to_descendant).
    A `visited` set is carried through the recursion to break cycles.
    """
    if path is None:
        path = [role]
    if visited is None:
        visited = {role}

    yield role, list(path)

    for child in children_index.get(role, []):
        if child in visited:
            continue
        yield from walk_descendants(
            child,
            children_index,
            path=path + [child],
            visited=visited | {child},
        )


## ----- CELL 6 — Build the (master, db, schema) -> dict mapping -----
def build_access_map(
    edges_df: pd.DataFrame,
    grants_df: pd.DataFrame,
    master_prefix: str = "FARMERS",
    verbose: bool = False,
) -> Dict[Tuple[str, str, str], dict]:

    # 6.1 adjacency dict — parent -> [children]
    children_index: Dict[str, List[str]] = defaultdict(list)
    for _, row in edges_df.iterrows():
        children_index[row["PARENT_ROLE"]].append(row["CHILD_ROLE"])

    # 6.2 role -> [(db, schema, privilege), ...]
    grants_index: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    for _, row in grants_df.iterrows():
        grants_index[row["ROLE_NAME"]].append(
            (row["DATABASE_NAME"], row["SCHEMA_NAME"], row["PRIVILEGE"])
        )

    # 6.3 master roles
    masters = sorted({
        p for p in edges_df["PARENT_ROLE"].unique()
        if str(p).upper().startswith(master_prefix.upper())
    })
    print(f"masters discovered : {len(masters)}")

    # 6.4 walk + accumulate
    result: Dict[Tuple[str, str, str], dict] = {}
    for master in masters:
        for descendant, path in walk_descendants(master, children_index):
            grants = grants_index.get(descendant, [])
            if not grants:
                continue
            # Group privileges per (db, schema) for this descendant
            per_dbsch: Dict[Tuple[str, str], List[str]] = defaultdict(list)
            for db, sch, priv in grants:
                per_dbsch[(db, sch)].append(priv)

            for (db, sch), privs in per_dbsch.items():
                privs = sorted(set(privs))
                key = (master, db, sch)
                if key not in result:
                    result[key] = {
                        "master_role": master,
                        "database":    db,
                        "schema":      sch,
                        "permissions": set(),
                        "paths":       [],
                    }
                result[key]["permissions"].update(privs)
                result[key]["paths"].append({
                    "descendant_role": descendant,
                    "depth":           len(path) - 1,
                    "role_path":       path,
                    "privileges":      privs,
                })
                if verbose:
                    print(f"  + {master} :: {descendant} via {path} -> {db}.{sch} {privs}")

    # 6.5 tidy sets / order the path list
    for val in result.values():
        val["permissions"] = sorted(val["permissions"])
        val["paths"].sort(key=lambda p: (p["depth"], p["descendant_role"]))

    return result

access_map = build_access_map(edges_df, grants_df, MASTER_PREFIX, verbose=False)
print(f"unique (master, db, schema) keys : {len(access_map):,}")


## ----- CELL 7 — Flatten dict to a DataFrame, ready for insert -----
records = []
for val in access_map.values():
    records.append({
        "MASTER_ROLE":   val["master_role"],
        "DATABASE_NAME": val["database"],
        "SCHEMA_NAME":   val["schema"],
        # store as JSON strings so write_pandas handles them safely; the
        # next cell will cast them to VARIANT during insert.
        "PERMISSIONS":   json.dumps(val["permissions"]),
        "CHILD_MAP":     json.dumps(val),
    })
out_df = pd.DataFrame(records)
print(f"rows ready to write : {len(out_df):,}")
out_df.head(10)


## ----- CELL 8 — Create the target table & MERGE rows in -----
# 8.1 target table with PK on (master_role, database_name, schema_name)
session.sql(f"""
    CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
        MASTER_ROLE     STRING       NOT NULL,
        DATABASE_NAME   STRING       NOT NULL,
        SCHEMA_NAME     STRING       NOT NULL,
        PERMISSIONS     VARIANT,
        CHILD_MAP       VARIANT,
        LOAD_TS         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
        CONSTRAINT PK_MASTER_DB_SCHEMA PRIMARY KEY (MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME)
    )
""").collect()

# 8.2 stage the DataFrame to a temp table (VARCHAR columns)
TMP_TABLE_FULL = f"{SOURCE_DB}.{SOURCE_SCHEMA}.FINAL_MASTER_ACCESS_MAP_PY_TMP"
session.write_pandas(
    out_df,
    table_name="FINAL_MASTER_ACCESS_MAP_PY_TMP",
    database=SOURCE_DB,
    schema=SOURCE_SCHEMA,
    auto_create_table=True,
    overwrite=True,
    quote_identifiers=False,
)
print(f"staged {len(out_df):,} rows into {TMP_TABLE_FULL}")

# 8.3 MERGE from the temp table to the final table, casting JSON -> VARIANT
session.sql(f"""
    MERGE INTO {TARGET_TABLE} tgt
    USING (
        SELECT MASTER_ROLE,
               DATABASE_NAME,
               SCHEMA_NAME,
               PARSE_JSON(PERMISSIONS) AS PERMISSIONS,
               PARSE_JSON(CHILD_MAP)   AS CHILD_MAP
        FROM   {TMP_TABLE_FULL}
    ) src
       ON tgt.MASTER_ROLE   = src.MASTER_ROLE
      AND tgt.DATABASE_NAME = src.DATABASE_NAME
      AND tgt.SCHEMA_NAME   = src.SCHEMA_NAME
    WHEN MATCHED THEN UPDATE SET
        tgt.PERMISSIONS = src.PERMISSIONS,
        tgt.CHILD_MAP   = src.CHILD_MAP,
        tgt.LOAD_TS     = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
         (MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME, PERMISSIONS, CHILD_MAP, LOAD_TS)
    VALUES
         (src.MASTER_ROLE, src.DATABASE_NAME, src.SCHEMA_NAME,
          src.PERMISSIONS, src.CHILD_MAP, CURRENT_TIMESTAMP())
""").collect()

session.sql(f"DROP TABLE IF EXISTS {TMP_TABLE_FULL}").collect()
print(f"MERGE complete -> {TARGET_TABLE}")


## ----- CELL 9 — Also save the detailed mapping as a JSON file in a stage -----
# Drop the dict as a JSON list and PUT it to the internal stage
records_for_json = [
    {
        "master_role": v["master_role"],
        "database":    v["database"],
        "schema":      v["schema"],
        "permissions": v["permissions"],
        "paths":       v["paths"],
    }
    for v in access_map.values()
]
local_json_path = "/tmp/farmers_access_map.json"
with open(local_json_path, "w") as f:
    json.dump(records_for_json, f, indent=2)

session.file.put(
    local_json_path,
    f"@{STAGE_NAME}/",
    auto_compress=False,
    overwrite=True,
)
print(f"JSON uploaded -> @{STAGE_NAME}/farmers_access_map.json")
session.sql(f"LIST @{STAGE_NAME}").show(20)


## ----- CELL 10 — Verification: read the final table back -----
verify_df = session.sql(f"""
    SELECT MASTER_ROLE,
           DATABASE_NAME,
           SCHEMA_NAME,
           PERMISSIONS,
           CHILD_MAP,
           LOAD_TS
    FROM   {TARGET_TABLE}
    ORDER  BY MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME
""").to_pandas()

print(f"rows in {TARGET_TABLE}: {len(verify_df):,}")
verify_df.head(20)


## ----- CELL 11 — Spot-checks: pretty-print one record + dup check -----
# 11a. Inspect one (master, db, schema) JSON in full
session.sql(f"""
    SELECT MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME,
           PERMISSIONS, CHILD_MAP
    FROM   {TARGET_TABLE}
    ORDER  BY MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME
    LIMIT  1
""").show(1, max_width=10000)

# 11b. Uniqueness check — must return 0 rows
dup_df = session.sql(f"""
    SELECT MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME, COUNT(*) AS DUP
    FROM   {TARGET_TABLE}
    GROUP  BY 1,2,3
    HAVING COUNT(*) > 1
""").to_pandas()
print(f"duplicate (master, db, schema) rows: {len(dup_df)}")
dup_df
