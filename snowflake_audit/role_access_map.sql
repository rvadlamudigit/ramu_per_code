-- =============================================================================
-- role_access_map.sql
--
-- Flatten Snowflake's role hierarchy into a (master_role, childmap, database,
-- schema) table. Walks SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES with a
-- recursive CTE: anchors at each "master role" (a role that is never granted
-- to any other role) and descends through every role-to-role grant, capturing
-- the chain as a JSON array. When the same master role can reach the same
-- database via multiple paths, you get one row per path (with different
-- chains in CHILDMAP).
--
-- Usage:
--     -- Run as ACCOUNTADMIN, or as a role with USAGE on
--     -- SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES.
--     -- ACCOUNT_USAGE has ~45-min latency; for live data switch the
--     -- source to a SHOW GRANTS / INFORMATION_SCHEMA approach.
--     !source role_access_map.sql
--
-- See test_data.sql for an offline test harness that mirrors the user's
-- example scenario without touching ACCOUNT_USAGE.
-- =============================================================================


-- ----------------------------------------------------------- target table ---
CREATE OR REPLACE TABLE ROLE_ACCESS_MAP (
    MASTER_ROLE   VARCHAR(255),
    CHILDMAP      VARIANT,            -- JSON describing the path
    DATABASE_NAME VARCHAR(255),
    SCHEMA_NAME   VARCHAR(255)
);

TRUNCATE TABLE ROLE_ACCESS_MAP;


-- ------------------------------------------------------------- insert dml ---
INSERT INTO ROLE_ACCESS_MAP (MASTER_ROLE, CHILDMAP, DATABASE_NAME, SCHEMA_NAME)
WITH RECURSIVE
-- ──────────────────────────────────────────────────────────────────────
-- 1. role -> role inheritance: parent_role INHERITS child_role's grants
-- ──────────────────────────────────────────────────────────────────────
role_grants AS (
    SELECT  GRANTEE_NAME AS parent_role,
            NAME         AS child_role
    FROM    SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
    WHERE   DELETED_ON IS NULL
      AND   GRANTED_ON = 'ROLE'
      AND   PRIVILEGE  = 'USAGE'
),
-- ──────────────────────────────────────────────────────────────────────
-- 2. direct DATABASE / SCHEMA grants on roles (the "leaf" privileges)
-- ──────────────────────────────────────────────────────────────────────
object_grants AS (
    SELECT  GRANTEE_NAME                            AS role_name,
            GRANTED_ON                              AS object_type,
            PRIVILEGE,
            CASE WHEN GRANTED_ON = 'DATABASE' THEN NAME
                 ELSE SPLIT_PART(NAME, '.', 1)
            END                                     AS database_name,
            CASE WHEN GRANTED_ON = 'SCHEMA'
                 THEN SPLIT_PART(NAME, '.', 2)
            END                                     AS schema_name
    FROM    SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
    WHERE   DELETED_ON IS NULL
      AND   GRANTED_ON IN ('DATABASE', 'SCHEMA')
      -- Add 'TABLE','VIEW','MATERIALIZED_VIEW' here if table-level access
      -- should also be flattened. SPLIT_PART already handles 3-part names.
),
-- ──────────────────────────────────────────────────────────────────────
-- 3. master roles = roles that are NEVER granted to any other role
-- ──────────────────────────────────────────────────────────────────────
master_roles AS (
    SELECT DISTINCT parent_role AS master_role
    FROM   role_grants
    WHERE  parent_role NOT IN (SELECT child_role FROM role_grants)
    -- Replace with an explicit list to limit scope, e.g.:
    --     SELECT 'MASTERROLE1' UNION ALL SELECT 'MASTERROLE2'
),
-- ──────────────────────────────────────────────────────────────────────
-- 4. recursive walk DOWN from each master role; record full path
-- ──────────────────────────────────────────────────────────────────────
role_chain AS (
    -- anchor: master role at depth 0, chain = [master]
    SELECT  m.master_role,
            m.master_role                                       AS current_role,
            ARRAY_CONSTRUCT(m.master_role)                      AS chain_path,
            0                                                   AS depth
    FROM    master_roles m

    UNION ALL

    -- recurse: descend one edge at a time, appending to the chain
    SELECT  rc.master_role,
            rg.child_role                                       AS current_role,
            ARRAY_APPEND(rc.chain_path, rg.child_role::VARIANT) AS chain_path,
            rc.depth + 1
    FROM    role_chain  rc
    JOIN    role_grants rg
      ON    rg.parent_role = rc.current_role
    WHERE   rc.depth < 20                                                  -- safety cap
      AND   NOT ARRAY_CONTAINS(rg.child_role::VARIANT, rc.chain_path)      -- cycle break
)
-- ──────────────────────────────────────────────────────────────────────
-- 5. final projection: one row per (master x path x db/schema grant)
-- ──────────────────────────────────────────────────────────────────────
SELECT
    rc.master_role,
    OBJECT_CONSTRUCT(
        'master_role', rc.master_role,
        'chain',       rc.chain_path,
        'leaf_role',   rc.current_role,
        'depth',       rc.depth,
        'object_type', og.object_type,
        'privilege',   og.privilege
    )                                              AS childmap,
    og.database_name,
    og.schema_name
FROM    role_chain  rc
JOIN    object_grants og
  ON    og.role_name = rc.current_role;


-- -------------------------------------------------------- verify queries ---
-- All paths for a given master role:
--     SELECT * FROM ROLE_ACCESS_MAP
--     WHERE MASTER_ROLE = 'MASTERROLE1'
--     ORDER BY DATABASE_NAME, SCHEMA_NAME, ARRAY_SIZE(CHILDMAP:chain);
--
-- Count of distinct paths per (master, database, schema):
--     SELECT MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME, COUNT(*) AS path_count
--     FROM   ROLE_ACCESS_MAP
--     GROUP  BY 1,2,3
--     HAVING COUNT(*) > 1
--     ORDER  BY path_count DESC;
--
-- Drill into the chain of one path:
--     SELECT CHILDMAP:chain::ARRAY AS chain,
--            CHILDMAP:privilege::STRING AS privilege
--     FROM   ROLE_ACCESS_MAP
--     WHERE  MASTER_ROLE = 'MASTERROLE1'
--       AND  DATABASE_NAME = 'DB1';
