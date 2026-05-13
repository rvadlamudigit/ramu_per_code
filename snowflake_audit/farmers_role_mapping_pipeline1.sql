/* =============================================================================
   FARMERS Role Access Mapping — Procedure with Persisted Staging Tables (v1)
   -----------------------------------------------------------------------------
   Reads role grants live from SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES,
   populates persistent staging tables (TRUNCATE + INSERT on every run), and
   produces a unique (master_role, database, schema) record set with a JSON
   child_map.

   DESIGN
   ------
   * No intermediate views. The procedure reads ACCOUNT_USAGE directly.
   * Staging tables live in FARMERS_AUDIT.RBAC and persist between runs so
     they can be inspected after the procedure finishes — every run
     TRUNCATES then re-INSERTs them.
   * FINAL_MASTER_ACCESS_MAP is upserted via MERGE so re-runs UPDATE in
     place (no PK violations).

   STEPS PERFORMED BY THE PROCEDURE
   --------------------------------
     1. TRUNCATE all STG_* tables (FINAL is untouched — MERGE handles it).
     2. STG_02_ROLE_EDGES        <- USAGE-on-ROLE grants from ACCOUNT_USAGE
     3. STG_03_ROLE_DB_GRANTS    <- DB/SCHEMA grants from ACCOUNT_USAGE
     4. STG_01_MASTER_ROLES      <- distinct parents matching MASTER_PREFIX
     5. STG_04_ROLE_PATHS        <- recursive walk from each master
     6. STG_05_MASTER_TO_DBSCHEMA <- paths joined to grants (granular fact)
     7. FINAL_MASTER_ACCESS_MAP  <- MERGE the aggregated/JSON rollup

   NOTE: ACCOUNT_USAGE has up to ~2 hours of latency.
   ========================================================================= */

USE ROLE       SECURITYADMIN;       -- needs ACCOUNT_USAGE access
USE WAREHOUSE  COMPUTE_WH;          -- adjust as required
CREATE DATABASE IF NOT EXISTS FARMERS_AUDIT;
CREATE SCHEMA   IF NOT EXISTS FARMERS_AUDIT.RBAC;
USE SCHEMA      FARMERS_AUDIT.RBAC;


/* =============================================================================
   SECTION 1  —  Persistent staging tables (created once, truncated each run)
   ========================================================================= */
CREATE TABLE IF NOT EXISTS STG_01_MASTER_ROLES (
    master_role STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS STG_02_ROLE_EDGES (
    parent_role STRING NOT NULL,
    child_role  STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS STG_03_ROLE_DB_GRANTS (
    role_name     STRING NOT NULL,
    database_name STRING NOT NULL,
    schema_name   STRING NOT NULL,
    privilege     STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS STG_04_ROLE_PATHS (
    master_role     STRING NOT NULL,
    descendant_role STRING NOT NULL,
    depth           INT,
    role_path       ARRAY
);

CREATE TABLE IF NOT EXISTS STG_05_MASTER_TO_DBSCHEMA (
    master_role     STRING NOT NULL,
    descendant_role STRING NOT NULL,
    depth           INT,
    role_path       ARRAY,
    database_name   STRING NOT NULL,
    schema_name     STRING NOT NULL,
    privilege       STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS FINAL_MASTER_ACCESS_MAP (
    MASTER_ROLE     STRING       NOT NULL,
    DATABASE_NAME   STRING       NOT NULL,
    SCHEMA_NAME     STRING       NOT NULL,
    PERMISSIONS     ARRAY,
    CHILD_MAP       VARIANT,
    LOAD_TS         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_MASTER_DB_SCHEMA PRIMARY KEY (MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME)
);


/* =============================================================================
   SECTION 2  —  Stored procedure
   -----------------------------------------------------------------------------
   BUILD_MASTER_ACCESS_MAP
     master_prefix   prefix that identifies master roles (default 'FARMERS')
     max_depth       safety cap on recursion depth (default 20)

   Returns a status string with per-table row counts and elapsed seconds.
   ========================================================================= */
CREATE OR REPLACE PROCEDURE BUILD_MASTER_ACCESS_MAP(
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
    -- ----- Step 1: Truncate all staging tables -----------------------------
    TRUNCATE TABLE STG_01_MASTER_ROLES;
    TRUNCATE TABLE STG_02_ROLE_EDGES;
    TRUNCATE TABLE STG_03_ROLE_DB_GRANTS;
    TRUNCATE TABLE STG_04_ROLE_PATHS;
    TRUNCATE TABLE STG_05_MASTER_TO_DBSCHEMA;

    -- ----- Step 2: STG_02_ROLE_EDGES <- ACCOUNT_USAGE ----------------------
    INSERT INTO STG_02_ROLE_EDGES (parent_role, child_role)
    SELECT GRANTEE_NAME, NAME
    FROM   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
    WHERE  GRANTED_ON = 'ROLE'
      AND  PRIVILEGE  = 'USAGE'
      AND  DELETED_ON IS NULL;

    SELECT COUNT(*) INTO :rows_stg2 FROM STG_02_ROLE_EDGES;

    -- ----- Step 3: STG_03_ROLE_DB_GRANTS <- ACCOUNT_USAGE ------------------
    INSERT INTO STG_03_ROLE_DB_GRANTS (role_name, database_name, schema_name, privilege)
    SELECT GRANTEE_NAME,
           TABLE_CATALOG,
           COALESCE(TABLE_SCHEMA, '*'),
           PRIVILEGE
    FROM   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
    WHERE  GRANTED_ON IN ('DATABASE','SCHEMA')
      AND  DELETED_ON IS NULL
      AND  TABLE_CATALOG IS NOT NULL;

    SELECT COUNT(*) INTO :rows_stg3 FROM STG_03_ROLE_DB_GRANTS;

    -- ----- Step 4: STG_01_MASTER_ROLES <- masters matching prefix ----------
    INSERT INTO STG_01_MASTER_ROLES (master_role)
    SELECT DISTINCT parent_role
    FROM   STG_02_ROLE_EDGES
    WHERE  parent_role ILIKE :MASTER_PREFIX || '%';

    SELECT COUNT(*) INTO :rows_stg1 FROM STG_01_MASTER_ROLES;

    -- ----- Step 5: STG_04_ROLE_PATHS <- recursive walk ---------------------
    INSERT INTO STG_04_ROLE_PATHS (master_role, descendant_role, depth, role_path)
    WITH RECURSIVE walk AS (
        SELECT  m.master_role,
                m.master_role                    AS role_name,
                0                                AS depth,
                ARRAY_CONSTRUCT(m.master_role)   AS role_path
        FROM    STG_01_MASTER_ROLES m

        UNION ALL

        SELECT  w.master_role,
                e.child_role,
                w.depth + 1,
                ARRAY_APPEND(w.role_path, e.child_role)
        FROM    walk w
        JOIN    STG_02_ROLE_EDGES e
               ON e.parent_role = w.role_name
        WHERE   w.depth < :MAX_DEPTH
          AND   ARRAY_POSITION(e.child_role::VARIANT, w.role_path) IS NULL
    )
    SELECT master_role, role_name, depth, role_path
    FROM   walk;

    SELECT COUNT(*) INTO :rows_stg4 FROM STG_04_ROLE_PATHS;

    -- ----- Step 6: STG_05_MASTER_TO_DBSCHEMA <- paths joined to grants -----
    INSERT INTO STG_05_MASTER_TO_DBSCHEMA
        (master_role, descendant_role, depth, role_path,
         database_name, schema_name, privilege)
    SELECT  p.master_role,
            p.descendant_role,
            p.depth,
            p.role_path,
            g.database_name,
            g.schema_name,
            g.privilege
    FROM    STG_04_ROLE_PATHS p
    JOIN    STG_03_ROLE_DB_GRANTS g
           ON g.role_name = p.descendant_role;

    SELECT COUNT(*) INTO :rows_stg5 FROM STG_05_MASTER_TO_DBSCHEMA;

    -- ----- Step 7: FINAL_MASTER_ACCESS_MAP <- MERGE rollup -----------------
    MERGE INTO FINAL_MASTER_ACCESS_MAP tgt
    USING (
        WITH per_descendant AS (
            SELECT  master_role,
                    database_name,
                    schema_name,
                    descendant_role,
                    depth,
                    role_path,
                    ARRAY_AGG(DISTINCT privilege)
                        WITHIN GROUP (ORDER BY privilege)   AS privs
            FROM    STG_05_MASTER_TO_DBSCHEMA
            GROUP   BY 1,2,3,4,5,6
        )
        SELECT
            master_role,
            database_name,
            schema_name,
            ARRAY_AGG(DISTINCT p.value::STRING)
                WITHIN GROUP (ORDER BY p.value::STRING)     AS permissions,
            OBJECT_CONSTRUCT(
                'master_role', master_role,
                'database',    database_name,
                'schema',      schema_name,
                'paths',
                    ARRAY_AGG(
                        OBJECT_CONSTRUCT(
                            'descendant_role', descendant_role,
                            'depth',           depth,
                            'role_path',       role_path,
                            'privileges',      privs
                        )
                    ) WITHIN GROUP (ORDER BY depth, descendant_role)
            )                                               AS child_map
        FROM   per_descendant,
               LATERAL FLATTEN(input => privs) p
        GROUP  BY master_role, database_name, schema_name
    ) src
    ON  tgt.MASTER_ROLE   = src.master_role
    AND tgt.DATABASE_NAME = src.database_name
    AND tgt.SCHEMA_NAME   = src.schema_name
    WHEN MATCHED THEN UPDATE SET
        tgt.PERMISSIONS = src.permissions,
        tgt.CHILD_MAP   = src.child_map,
        tgt.LOAD_TS     = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME, PERMISSIONS, CHILD_MAP, LOAD_TS)
    VALUES
        (src.master_role, src.database_name, src.schema_name,
         src.permissions, src.child_map, CURRENT_TIMESTAMP());

    SELECT COUNT(*) INTO :rows_final FROM FINAL_MASTER_ACCESS_MAP;
    elapsed_s := DATEDIFF('millisecond', :started_at, CURRENT_TIMESTAMP())/1000.0;

    RETURN 'OK — '
        || 'stg01_masters='   || :rows_stg1
        || ', stg02_edges='   || :rows_stg2
        || ', stg03_grants='  || :rows_stg3
        || ', stg04_paths='   || :rows_stg4
        || ', stg05_fact='    || :rows_stg5
        || ', final_rows='    || :rows_final
        || ', elapsed='       || :elapsed_s || 's';
END;
$$;


/* =============================================================================
   SECTION 3  —  Run it
   ========================================================================= */
CALL BUILD_MASTER_ACCESS_MAP();                            -- defaults
-- CALL BUILD_MASTER_ACCESS_MAP('FARMERS', 30);            -- custom args


/* =============================================================================
   SECTION 4  —  Verification (run any of these after the procedure call)
   ========================================================================= */
-- 4a. Inspect each staging table (still populated for post-mortem)
SELECT * FROM STG_01_MASTER_ROLES         ORDER BY master_role;
SELECT * FROM STG_02_ROLE_EDGES           ORDER BY parent_role, child_role;
SELECT * FROM STG_03_ROLE_DB_GRANTS       ORDER BY role_name, database_name, schema_name;
SELECT * FROM STG_04_ROLE_PATHS           ORDER BY master_role, depth, descendant_role;
SELECT * FROM STG_05_MASTER_TO_DBSCHEMA   ORDER BY master_role, database_name, schema_name;

-- 4b. Final table preview
SELECT MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME, PERMISSIONS, CHILD_MAP, LOAD_TS
FROM   FINAL_MASTER_ACCESS_MAP
ORDER  BY MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME;

-- 4c. Uniqueness check (must return 0 rows)
SELECT MASTER_ROLE, DATABASE_NAME, SCHEMA_NAME, COUNT(*) AS dup
FROM   FINAL_MASTER_ACCESS_MAP
GROUP  BY 1,2,3
HAVING COUNT(*) > 1;

-- 4d. Flatten the JSON back into columns
SELECT  m.MASTER_ROLE,
        m.DATABASE_NAME,
        m.SCHEMA_NAME,
        p.value:descendant_role::STRING AS descendant_role,
        p.value:depth::INT              AS depth,
        p.value:role_path               AS role_path,
        p.value:privileges              AS privileges
FROM    FINAL_MASTER_ACCESS_MAP m,
        LATERAL FLATTEN(input => m.CHILD_MAP:paths) p
ORDER   BY m.MASTER_ROLE, m.DATABASE_NAME, m.SCHEMA_NAME, depth, descendant_role;
