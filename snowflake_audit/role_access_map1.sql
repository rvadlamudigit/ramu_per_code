/* =============================================================================
   Snowflake Role Audit — Master Role to Database/Schema Permission Mapping
   -----------------------------------------------------------------------------
   Purpose:
     For every "master" role (name begins with FARMERS*), walk down the
     role-hierarchy through any number of child layers and produce one row
     per UNIQUE (master_role, database, schema) combination containing:
        - consolidated list of privileges granted at that DB/Schema
        - JSON child_map describing every child role in the chain that
          contributes a grant to that DB/Schema, including its privileges
          and depth from the master.

   Source of truth:
     SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
       (latency up to ~2 hours; switch to INFORMATION_SCHEMA or live
        SHOW GRANTS if you need real-time data.)

   Assumptions:
     - Master role naming convention: NAME LIKE 'FARMERS%'
     - Role-to-role membership is recorded as
         GRANTED_ON = 'ROLE', PRIVILEGE = 'USAGE'
         where GRANTEE_NAME = parent role, NAME = child role.
     - DB/Schema grants are rows with GRANTED_ON IN ('DATABASE','SCHEMA').
   ========================================================================= */

USE ROLE       SECURITYADMIN;     -- or any role that can read ACCOUNT_USAGE
USE WAREHOUSE  COMPUTE_WH;        -- adjust as required
USE DATABASE   SNOWFLAKE;
USE SCHEMA     ACCOUNT_USAGE;

/* -----------------------------------------------------------------------------
   1. Persist the result in an audit table so it can be queried / exported.
   -------------------------------------------------------------------------- */
CREATE DATABASE IF NOT EXISTS FARMERS_AUDIT;
CREATE SCHEMA   IF NOT EXISTS FARMERS_AUDIT.RBAC;

CREATE OR REPLACE TABLE FARMERS_AUDIT.RBAC.MASTER_ROLE_PERMISSION_MAP (
    MASTER_ROLE   STRING       NOT NULL,
    DATABASE_NAME STRING       NOT NULL,
    SCHEMA_NAME   STRING       NOT NULL,
    PERMISSIONS   ARRAY,            -- distinct privileges at DB+SCHEMA level
    CHILD_MAP     VARIANT,          -- JSON tree from master -> ... -> db/schema
    LOAD_TS       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_MASTER_DB_SCHEMA  PRIMARY KEY (MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME)
);

/* -----------------------------------------------------------------------------
   2. Recursive walk of the role graph starting at every FARMERS* master.
      Captures the FULL path (role_path) from master down to each descendant.
   -------------------------------------------------------------------------- */
WITH RECURSIVE
role_membership AS (
    /* Direct parent -> child edges (USAGE on ROLE) */
    SELECT  GRANTEE_NAME  AS PARENT_ROLE,
            NAME          AS CHILD_ROLE
    FROM    SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
    WHERE   GRANTED_ON   = 'ROLE'
      AND   PRIVILEGE    = 'USAGE'
      AND   DELETED_ON  IS NULL
),
master_roles AS (
    SELECT DISTINCT NAME AS MASTER_ROLE
    FROM   SNOWFLAKE.ACCOUNT_USAGE.ROLES
    WHERE  DELETED_ON IS NULL
      AND  NAME LIKE 'FARMERS%'
),
role_tree AS (
    /* Anchor — master role itself (level 0, role_path = [master]) */
    SELECT  m.MASTER_ROLE,
            m.MASTER_ROLE                              AS ROLE_NAME,
            0                                          AS LEVEL,
            ARRAY_CONSTRUCT(m.MASTER_ROLE)             AS ROLE_PATH
    FROM    master_roles m

    UNION ALL

    /* Recursive — every direct child of a role already in the tree */
    SELECT  rt.MASTER_ROLE,
            rm.CHILD_ROLE                              AS ROLE_NAME,
            rt.LEVEL + 1                               AS LEVEL,
            ARRAY_APPEND(rt.ROLE_PATH, rm.CHILD_ROLE)  AS ROLE_PATH
    FROM    role_tree       rt
    JOIN    role_membership rm
           ON rm.PARENT_ROLE = rt.ROLE_NAME
    /* cycle guard */
    WHERE   ARRAY_POSITION(rm.CHILD_ROLE::VARIANT, rt.ROLE_PATH) IS NULL
),
/* -----------------------------------------------------------------------------
   3. Resolve every DB/SCHEMA grant for every role in every master's subtree.
   -------------------------------------------------------------------------- */
role_db_schema_grants AS (
    SELECT
        rt.MASTER_ROLE,
        rt.ROLE_NAME,
        rt.LEVEL,
        rt.ROLE_PATH,
        g.PRIVILEGE,
        g.GRANTED_ON,
        /* For DATABASE grants schema is recorded as NULL — bucket them under '*' */
        g.TABLE_CATALOG                             AS DATABASE_NAME,
        COALESCE(g.TABLE_SCHEMA, '*')               AS SCHEMA_NAME
    FROM   role_tree rt
    JOIN   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES g
           ON g.GRANTEE_NAME = rt.ROLE_NAME
    WHERE  g.DELETED_ON IS NULL
      AND  g.GRANTED_ON IN ('DATABASE','SCHEMA')
      AND  g.TABLE_CATALOG IS NOT NULL
),
/* -----------------------------------------------------------------------------
   4. Build per-child-role JSON describing its contribution at each DB/Schema.
   -------------------------------------------------------------------------- */
child_contribution AS (
    SELECT
        MASTER_ROLE,
        DATABASE_NAME,
        SCHEMA_NAME,
        ROLE_NAME,
        LEVEL,
        ROLE_PATH,
        ARRAY_AGG(DISTINCT PRIVILEGE) WITHIN GROUP (ORDER BY PRIVILEGE) AS PRIVS
    FROM   role_db_schema_grants
    GROUP BY MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME, ROLE_NAME, LEVEL, ROLE_PATH
),
/* -----------------------------------------------------------------------------
   5. Roll up to one row per (master_role, database, schema). The CHILD_MAP
      JSON contains the master plus the full list of contributing child roles
      (role, level, path-from-master, privileges).
   -------------------------------------------------------------------------- */
final_map AS (
    SELECT
        MASTER_ROLE,
        DATABASE_NAME,
        SCHEMA_NAME,
        ARRAY_AGG(DISTINCT priv.value::STRING) WITHIN GROUP (ORDER BY priv.value::STRING)
                                                                AS PERMISSIONS,
        OBJECT_CONSTRUCT(
            'master_role',  MASTER_ROLE,
            'database',     DATABASE_NAME,
            'schema',       SCHEMA_NAME,
            'children',
                ARRAY_AGG(
                    OBJECT_CONSTRUCT(
                        'role',       ROLE_NAME,
                        'level',      LEVEL,
                        'path',       ROLE_PATH,
                        'privileges', PRIVS
                    )
                ) WITHIN GROUP (ORDER BY LEVEL, ROLE_NAME)
        )                                                       AS CHILD_MAP
    FROM   child_contribution,
           LATERAL FLATTEN(input => PRIVS) priv
    GROUP BY MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME
)
/* -----------------------------------------------------------------------------
   6. MERGE to keep (master_role, database, schema) unique.
   -------------------------------------------------------------------------- */
MERGE INTO FARMERS_AUDIT.RBAC.MASTER_ROLE_PERMISSION_MAP tgt
USING        final_map                                            src
   ON  tgt.MASTER_ROLE   = src.MASTER_ROLE
   AND tgt.DATABASE_NAME = src.DATABASE_NAME
   AND tgt.SCHEMA_NAME   = src.SCHEMA_NAME
WHEN MATCHED THEN UPDATE SET
        tgt.PERMISSIONS = src.PERMISSIONS,
        tgt.CHILD_MAP   = src.CHILD_MAP,
        tgt.LOAD_TS     = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT
       (MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME, PERMISSIONS, CHILD_MAP, LOAD_TS)
VALUES (src.MASTER_ROLE, src.DATABASE_NAME, src.SCHEMA_NAME,
        src.PERMISSIONS, src.CHILD_MAP, CURRENT_TIMESTAMP());

/* -----------------------------------------------------------------------------
   7. Quick verification queries
   -------------------------------------------------------------------------- */
-- 7a. Row count check
SELECT COUNT(*) AS row_count
FROM   FARMERS_AUDIT.RBAC.MASTER_ROLE_PERMISSION_MAP;

-- 7b. Sample preview
SELECT  MASTER_ROLE,
        DATABASE_NAME,
        SCHEMA_NAME,
        PERMISSIONS,
        CHILD_MAP
FROM    FARMERS_AUDIT.RBAC.MASTER_ROLE_PERMISSION_MAP
ORDER BY MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME;

-- 7c. Uniqueness check (must return 0 rows)
SELECT  MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME, COUNT(*) AS dup_cnt
FROM    FARMERS_AUDIT.RBAC.MASTER_ROLE_PERMISSION_MAP
GROUP BY 1,2,3
HAVING  COUNT(*) > 1;

/* -----------------------------------------------------------------------------
   8. Export to a JSON file (one record per master+db+schema).
      Adjust the stage / file format / path to your environment.
   -------------------------------------------------------------------------- */
-- Optional: create a stage you can write to
CREATE STAGE IF NOT EXISTS FARMERS_AUDIT.RBAC.AUDIT_STAGE
    FILE_FORMAT = (TYPE = JSON);

COPY INTO @FARMERS_AUDIT.RBAC.AUDIT_STAGE/master_role_permission_map.json
FROM (
    SELECT OBJECT_CONSTRUCT(
              'master_role',   MASTER_ROLE,
              'database',      DATABASE_NAME,
              'schema',        SCHEMA_NAME,
              'permissions',   PERMISSIONS,
              'child_map',     CHILD_MAP,
              'generated_at',  LOAD_TS
           )
    FROM   FARMERS_AUDIT.RBAC.MASTER_ROLE_PERMISSION_MAP
)
FILE_FORMAT  = (TYPE = JSON, COMPRESSION = NONE)
OVERWRITE    = TRUE
SINGLE       = TRUE
HEADER       = FALSE
MAX_FILE_SIZE = 5368709120;

-- To pull the JSON to your laptop:
--   GET @FARMERS_AUDIT.RBAC.AUDIT_STAGE/master_role_permission_map.json
--       file:///Users/ramuvadlamudi/git/ramu_per_code/snowflake_audit/;

