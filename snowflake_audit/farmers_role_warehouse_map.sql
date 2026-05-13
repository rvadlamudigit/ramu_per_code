/* =============================================================================
   FARMERS — Master Role to Warehouse Mapping (with full role-chain JSON)
   -----------------------------------------------------------------------------
   PRODUCES one row per FARMERS* master role:

        col 1  MASTER_ROLE         STRING
        col 2  WAREHOUSES          ARRAY      -- distinct list of warehouses
        col 3  WAREHOUSE_MAP       VARIANT    -- detailed JSON of every path
                                              --   master -> child -> ... -> WH

   JSON shape (WAREHOUSE_MAP)
   --------------------------
   {
     "master_role" : "FARMERS_MASTER1",
     "warehouses"  : ["WH_A","WH_B"],
     "paths": [
       { "warehouse":"WH_A", "privilege":"USAGE",
         "via_role" :"CHILD1", "depth":1,
         "role_path":["FARMERS_MASTER1","CHILD1"],
         "path_string":"FARMERS_MASTER1 -> CHILD1 : WH_A" },
       { "warehouse":"WH_B", "privilege":"USAGE",
         "via_role" :"CHILD2", "depth":2,
         "role_path":["FARMERS_MASTER1","CHILD1","CHILD2"],
         "path_string":"FARMERS_MASTER1 -> CHILD1 -> CHILD2 : WH_B" }
     ]
   }

   SOURCE: SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES (latency up to ~2 hours)
   ========================================================================= */

USE ROLE       SECURITYADMIN;
USE WAREHOUSE  COMPUTE_WH;
CREATE DATABASE IF NOT EXISTS FARMERS_AUDIT;
CREATE SCHEMA   IF NOT EXISTS FARMERS_AUDIT.RBAC;
USE SCHEMA      FARMERS_AUDIT.RBAC;


/* =============================================================================
   SECTION 1  —  Persistent staging tables (truncated each run, inspectable)
   ========================================================================= */
CREATE TABLE IF NOT EXISTS STG_W_01_MASTERS (
    master_role  STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS STG_W_02_ROLE_EDGES (
    parent_role  STRING NOT NULL,
    child_role   STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS STG_W_03_WAREHOUSE_GRANTS (
    role_name       STRING NOT NULL,
    warehouse_name  STRING NOT NULL,
    privilege       STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS STG_W_04_ROLE_PATHS (
    master_role     STRING NOT NULL,
    descendant_role STRING NOT NULL,
    depth           INT,
    role_path       ARRAY
);

CREATE TABLE IF NOT EXISTS STG_W_05_MASTER_TO_WAREHOUSE (
    master_role     STRING NOT NULL,
    descendant_role STRING NOT NULL,
    depth           INT,
    role_path       ARRAY,
    warehouse_name  STRING NOT NULL,
    privilege       STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS FARMERS_MASTER_WAREHOUSE_MAP (
    MASTER_ROLE     STRING        NOT NULL,
    WAREHOUSES      ARRAY,
    WAREHOUSE_MAP   VARIANT,
    LOAD_TS         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_FMW_MASTER PRIMARY KEY (MASTER_ROLE)
);


/* =============================================================================
   SECTION 2  —  Stored procedure
   ========================================================================= */
CREATE OR REPLACE PROCEDURE BUILD_MASTER_WAREHOUSE_MAP(
    MASTER_PREFIX STRING DEFAULT 'FARMERS',
    MAX_DEPTH     INTEGER DEFAULT 20
)
RETURNS STRING
LANGUAGE SQL
AS
$$
DECLARE
    rows_stg1  INT;
    rows_stg2  INT;
    rows_stg3  INT;
    rows_stg4  INT;
    rows_stg5  INT;
    rows_final INT;
    started_at TIMESTAMP_LTZ := CURRENT_TIMESTAMP();
    elapsed_s  FLOAT;
BEGIN
    /* ---- Step 1: truncate all staging tables ---------------------------- */
    TRUNCATE TABLE STG_W_01_MASTERS;
    TRUNCATE TABLE STG_W_02_ROLE_EDGES;
    TRUNCATE TABLE STG_W_03_WAREHOUSE_GRANTS;
    TRUNCATE TABLE STG_W_04_ROLE_PATHS;
    TRUNCATE TABLE STG_W_05_MASTER_TO_WAREHOUSE;

    /* ---- Step 2: role -> role edges ------------------------------------- */
    INSERT INTO STG_W_02_ROLE_EDGES (parent_role, child_role)
    SELECT GRANTEE_NAME, NAME
    FROM   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
    WHERE  GRANTED_ON = 'ROLE'
      AND  PRIVILEGE  = 'USAGE'
      AND  DELETED_ON IS NULL;

    SELECT COUNT(*) INTO :rows_stg2 FROM STG_W_02_ROLE_EDGES;

    /* ---- Step 3: role -> warehouse grants ------------------------------- */
    INSERT INTO STG_W_03_WAREHOUSE_GRANTS (role_name, warehouse_name, privilege)
    SELECT GRANTEE_NAME, NAME, PRIVILEGE
    FROM   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
    WHERE  GRANTED_ON = 'WAREHOUSE'
      AND  DELETED_ON IS NULL;

    SELECT COUNT(*) INTO :rows_stg3 FROM STG_W_03_WAREHOUSE_GRANTS;

    /* ---- Step 4: master roles ------------------------------------------- */
    INSERT INTO STG_W_01_MASTERS (master_role)
    SELECT DISTINCT parent_role
    FROM   STG_W_02_ROLE_EDGES
    WHERE  parent_role ILIKE :MASTER_PREFIX || '%';

    SELECT COUNT(*) INTO :rows_stg1 FROM STG_W_01_MASTERS;

    /* ---- Step 5: recursive walk: master -> ... -> descendant ------------ */
    INSERT INTO STG_W_04_ROLE_PATHS (master_role, descendant_role, depth, role_path)
    WITH RECURSIVE walk AS (
        SELECT  m.master_role,
                m.master_role                       AS role_name,
                0                                   AS depth,
                ARRAY_CONSTRUCT(m.master_role)      AS role_path
        FROM    STG_W_01_MASTERS m

        UNION ALL

        SELECT  w.master_role,
                e.child_role,
                w.depth + 1,
                ARRAY_APPEND(w.role_path, e.child_role)
        FROM    walk w
        JOIN    STG_W_02_ROLE_EDGES e
               ON e.parent_role = w.role_name
        WHERE   w.depth < :MAX_DEPTH
          AND   ARRAY_POSITION(e.child_role::VARIANT, w.role_path) IS NULL
    )
    SELECT master_role, role_name, depth, role_path
    FROM   walk;

    SELECT COUNT(*) INTO :rows_stg4 FROM STG_W_04_ROLE_PATHS;

    /* ---- Step 6: join paths to warehouse grants ------------------------- */
    INSERT INTO STG_W_05_MASTER_TO_WAREHOUSE
        (master_role, descendant_role, depth, role_path,
         warehouse_name, privilege)
    SELECT  p.master_role,
            p.descendant_role,
            p.depth,
            p.role_path,
            g.warehouse_name,
            g.privilege
    FROM    STG_W_04_ROLE_PATHS  p
    JOIN    STG_W_03_WAREHOUSE_GRANTS g
           ON g.role_name = p.descendant_role;

    SELECT COUNT(*) INTO :rows_stg5 FROM STG_W_05_MASTER_TO_WAREHOUSE;

    /* ---- Step 7: MERGE into the final aggregated table ------------------ */
    MERGE INTO FARMERS_MASTER_WAREHOUSE_MAP tgt
    USING (
        WITH path_rows AS (
            SELECT
                master_role,
                warehouse_name,
                descendant_role,
                depth,
                role_path,
                privilege,
                -- pretty arrow string like "MASTER -> CHILD1 -> CHILD2 : WH_X"
                ARRAY_TO_STRING(role_path, ' -> ')
                    || ' : ' || warehouse_name                AS path_string
            FROM    STG_W_05_MASTER_TO_WAREHOUSE
        )
        SELECT
            master_role,
            ARRAY_AGG(DISTINCT warehouse_name)
                WITHIN GROUP (ORDER BY warehouse_name)        AS warehouses,
            OBJECT_CONSTRUCT(
                'master_role', master_role,
                'warehouses',
                    ARRAY_AGG(DISTINCT warehouse_name)
                        WITHIN GROUP (ORDER BY warehouse_name),
                'paths',
                    ARRAY_AGG(
                        OBJECT_CONSTRUCT(
                            'warehouse',   warehouse_name,
                            'privilege',   privilege,
                            'via_role',    descendant_role,
                            'depth',       depth,
                            'role_path',   role_path,
                            'path_string', path_string
                        )
                    ) WITHIN GROUP (ORDER BY warehouse_name, depth, descendant_role)
            )                                                  AS warehouse_map
        FROM   path_rows
        GROUP  BY master_role
    ) src
    ON  tgt.MASTER_ROLE = src.master_role
    WHEN MATCHED THEN UPDATE SET
        tgt.WAREHOUSES    = src.warehouses,
        tgt.WAREHOUSE_MAP = src.warehouse_map,
        tgt.LOAD_TS       = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (MASTER_ROLE, WAREHOUSES, WAREHOUSE_MAP, LOAD_TS)
    VALUES
        (src.master_role, src.warehouses, src.warehouse_map, CURRENT_TIMESTAMP());

    SELECT COUNT(*) INTO :rows_final FROM FARMERS_MASTER_WAREHOUSE_MAP;
    elapsed_s := DATEDIFF('millisecond', :started_at, CURRENT_TIMESTAMP())/1000.0;

    RETURN 'OK — '
        || 'stg01_masters='   || :rows_stg1
        || ', stg02_edges='   || :rows_stg2
        || ', stg03_wh_grants='|| :rows_stg3
        || ', stg04_paths='   || :rows_stg4
        || ', stg05_fact='    || :rows_stg5
        || ', final_rows='    || :rows_final
        || ', elapsed='       || :elapsed_s || 's';
END;
$$;


/* =============================================================================
   SECTION 3  —  Run it
   ========================================================================= */
CALL BUILD_MASTER_WAREHOUSE_MAP();                       -- defaults
-- CALL BUILD_MASTER_WAREHOUSE_MAP('FARMERS', 30);       -- custom prefix / depth


/* =============================================================================
   SECTION 4  —  Verification
   ========================================================================= */
-- 4a. staging-table peeks (post-mortem debug)
SELECT * FROM STG_W_01_MASTERS              ORDER BY master_role;
SELECT * FROM STG_W_02_ROLE_EDGES           ORDER BY parent_role, child_role;
SELECT * FROM STG_W_03_WAREHOUSE_GRANTS     ORDER BY role_name, warehouse_name;
SELECT * FROM STG_W_04_ROLE_PATHS           ORDER BY master_role, depth, descendant_role;
SELECT * FROM STG_W_05_MASTER_TO_WAREHOUSE  ORDER BY master_role, warehouse_name, depth;

-- 4b. final table — the deliverable
SELECT MASTER_ROLE, WAREHOUSES, WAREHOUSE_MAP, LOAD_TS
FROM   FARMERS_MASTER_WAREHOUSE_MAP
ORDER  BY MASTER_ROLE;

-- 4c. uniqueness check (must return 0 rows)
SELECT MASTER_ROLE, COUNT(*) AS dup
FROM   FARMERS_MASTER_WAREHOUSE_MAP
GROUP  BY 1
HAVING COUNT(*) > 1;

-- 4d. flatten the JSON for human review (one row per path)
SELECT  m.MASTER_ROLE,
        m.WAREHOUSES,
        p.value:warehouse::STRING    AS warehouse,
        p.value:privilege::STRING    AS privilege,
        p.value:via_role::STRING     AS via_role,
        p.value:depth::INT           AS depth,
        p.value:role_path            AS role_path,
        p.value:path_string::STRING  AS path_string
FROM    FARMERS_MASTER_WAREHOUSE_MAP m,
        LATERAL FLATTEN(input => m.WAREHOUSE_MAP:paths) p
ORDER   BY m.MASTER_ROLE, warehouse, depth, via_role;
