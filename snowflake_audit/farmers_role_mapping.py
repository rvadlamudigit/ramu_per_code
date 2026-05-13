"""
FARMERS Role Mapping — Python / pandas Implementation
=====================================================
Reads the role-edge and DB/Schema-grant tables into pandas DataFrames,
runs a recursive walker from every FARMERS* master role through every
descendant, and produces a dict object keyed by
        (master_role, database_name, schema_name)

For every key the value contains:
    * master_role         : the FARMERS* master role
    * database            : database name
    * schema              : schema name
    * permissions         : consolidated, deduped privilege list
    * paths               : every child-role path that contributed access,
                            each with role_path, depth, and that path's privs

The script is fully debuggable:
    * Each stage prints the intermediate state of the dataframe / dict.
    * Test data is embedded so the script runs standalone (no Snowflake
      connection needed). A `load_from_snowflake` function shows how to
      swap in real ACCOUNT_USAGE data.
"""

import json
from collections import defaultdict
from typing import Dict, Generator, List, Optional, Tuple

import pandas as pd


# =============================================================================
# SECTION 1 — Data loaders (test data + optional Snowflake)
# =============================================================================
def get_test_data() -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Mirrors the test data in farmers_role_mapping_pipeline.sql so the Python
    output can be cross-checked against the SQL output.
    """
    edges = pd.DataFrame(
        [
            ("FARMERS_MASTER1", "CHILD1"),
            ("FARMERS_MASTER1", "CHILD10"),
            ("FARMERS_MASTER2", "CHILD10"),
            ("CHILD1",          "CHILD2"),
            ("CHILD2",          "CHILD3"),
            ("CHILD2",          "CHILD4"),
            ("CHILD10",         "CHILD5"),
        ],
        columns=["parent_role", "child_role"],
    )
    grants = pd.DataFrame(
        [
            ("CHILD3", "DB1", "SCH1", "USAGE"),
            ("CHILD3", "DB1", "SCH1", "SELECT"),
            ("CHILD4", "DB2", "SCH2", "USAGE"),
            ("CHILD5", "DB1", "SCH1", "USAGE"),
            ("CHILD5", "DB3", "SCH3", "SELECT"),
        ],
        columns=["role_name", "database_name", "schema_name", "privilege"],
    )
    return edges, grants


def load_from_snowflake(conn) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    OPTIONAL — pull live data from ACCOUNT_USAGE.
    `conn` is a snowflake.connector connection or a sqlalchemy engine.
    """
    edges_sql = """
        SELECT GRANTEE_NAME AS parent_role,
               NAME         AS child_role
        FROM   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
        WHERE  GRANTED_ON = 'ROLE'
          AND  PRIVILEGE  = 'USAGE'
          AND  DELETED_ON IS NULL
    """
    grants_sql = """
        SELECT GRANTEE_NAME                       AS role_name,
               TABLE_CATALOG                      AS database_name,
               COALESCE(TABLE_SCHEMA, '*')        AS schema_name,
               PRIVILEGE                          AS privilege
        FROM   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
        WHERE  GRANTED_ON IN ('DATABASE','SCHEMA')
          AND  DELETED_ON IS NULL
    """
    edges  = pd.read_sql(edges_sql,  conn)
    grants = pd.read_sql(grants_sql, conn)
    return edges, grants


# =============================================================================
# SECTION 2 — Recursive walker
# =============================================================================
def walk_descendants(
    role: str,
    children_index: Dict[str, List[str]],
    path: Optional[List[str]] = None,
    visited: Optional[set] = None,
) -> Generator[Tuple[str, List[str]], None, None]:
    """
    Yield (descendant_role, full_path_from_master_to_descendant) for `role`
    and every reachable descendant.

    `children_index` is a dict { parent -> [child, child, ...] } built once
    from the edges dataframe so we don't filter the dataframe inside the
    recursion (much faster, easier to read).

    Cycle guard: a role appearing twice on the same path is skipped.
    """
    if path is None:
        path = [role]
    if visited is None:
        visited = {role}

    # Emit the current role first (depth-first pre-order).
    yield role, list(path)

    for child in children_index.get(role, []):
        if child in visited:
            continue  # cycle — skip
        yield from walk_descendants(
            child,
            children_index,
            path=path + [child],
            visited=visited | {child},
        )


# =============================================================================
# SECTION 3 — Pipeline (DataFrame -> dict)
# =============================================================================
def build_access_map(
    edges_df: pd.DataFrame,
    grants_df: pd.DataFrame,
    master_prefix: str = "FARMERS",
    verbose: bool = True,
) -> Dict[Tuple[str, str, str], dict]:
    """
    Walk every FARMERS* master and return a dict keyed by
        (master_role, database_name, schema_name)
    with a value that contains permissions + every contributing path.
    """
    # ---- 3.1 build a parent->children adjacency dict from the edges df ------
    children_index: Dict[str, List[str]] = defaultdict(list)
    for _, row in edges_df.iterrows():
        children_index[row["parent_role"]].append(row["child_role"])

    # ---- 3.2 build a role -> [(db, schema, privilege), ...] index -----------
    grants_index: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    for _, row in grants_df.iterrows():
        grants_index[row["role_name"]].append(
            (row["database_name"], row["schema_name"], row["privilege"])
        )

    # ---- 3.3 identify masters ---------------------------------------------
    masters = sorted(
        {p for p in edges_df["parent_role"].unique()
         if p.upper().startswith(master_prefix.upper())}
    )
    if verbose:
        print(f"[stage 3.3] master roles: {masters}")

    # ---- 3.4 walk + accumulate --------------------------------------------
    # We use a temporary structure with sets/lists; we'll tidy it at the end.
    tmp: Dict[Tuple[str, str, str], dict] = {}

    for master in masters:
        if verbose:
            print(f"\n[stage 3.4] walking master = {master}")

        for descendant, path in walk_descendants(master, children_index):
            grants_for_role = grants_index.get(descendant, [])
            if not grants_for_role:
                if verbose:
                    print(f"   - {descendant} (depth {len(path)-1}): no DB/schema grants")
                continue

            # Group privileges by (db, schema) for this descendant.
            per_dbsch: Dict[Tuple[str, str], List[str]] = defaultdict(list)
            for db, sch, priv in grants_for_role:
                per_dbsch[(db, sch)].append(priv)

            for (db, sch), privs in per_dbsch.items():
                privs = sorted(set(privs))
                key = (master, db, sch)

                if key not in tmp:
                    tmp[key] = {
                        "master_role": master,
                        "database":    db,
                        "schema":      sch,
                        "permissions": set(),
                        "paths":       [],
                    }
                tmp[key]["permissions"].update(privs)
                tmp[key]["paths"].append(
                    {
                        "descendant_role": descendant,
                        "depth":           len(path) - 1,
                        "role_path":       path,
                        "privileges":      privs,
                    }
                )

                if verbose:
                    print(
                        f"   + {descendant} via {' -> '.join(path)} "
                        f"=> {db}.{sch} {privs}"
                    )

    # ---- 3.5 convert sets to sorted lists, sort path list ------------------
    for key, val in tmp.items():
        val["permissions"] = sorted(val["permissions"])
        val["paths"].sort(key=lambda p: (p["depth"], p["descendant_role"]))

    return tmp


# =============================================================================
# SECTION 4 — Pretty printers / exporters
# =============================================================================
def print_dataframe(name: str, df: pd.DataFrame) -> None:
    print(f"\n=== {name} ({len(df)} rows) ===")
    print(df.to_string(index=False))


def print_access_map(access_map: Dict[Tuple[str, str, str], dict]) -> None:
    print(f"\n=== FINAL DICT ({len(access_map)} unique master+db+schema rows) ===")
    for key in sorted(access_map):
        print(f"\nKey {key}")
        print(json.dumps(access_map[key], indent=2))


def export_access_map_json(
    access_map: Dict[Tuple[str, str, str], dict],
    path: str,
) -> None:
    # json keys must be strings — emit as a list of dicts which is friendlier anyway
    records = [access_map[k] for k in sorted(access_map)]
    with open(path, "w") as f:
        json.dump(records, f, indent=2)
    print(f"\n[exported] {len(records)} records -> {path}")


# =============================================================================
# SECTION 5 — Main
# =============================================================================
def main() -> None:
    edges, grants = get_test_data()

    print_dataframe("STAGE 1: edges", edges)
    print_dataframe("STAGE 2: grants", grants)

    access_map = build_access_map(edges, grants, master_prefix="FARMERS")

    print_access_map(access_map)
    export_access_map_json(access_map, "farmers_access_map.json")


if __name__ == "__main__":
    main()
