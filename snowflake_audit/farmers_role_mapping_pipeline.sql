/* =============================================================================
   FARMERS Role Access Mapping — Detailed, Debug-Friendly Pipeline
   -----------------------------------------------------------------------------
   GOAL
   ----
   For every "master" role whose name starts with FARMERS, walk through every
   child role (any depth, any branching: 1-1 / 1-many / many-1) and find the
   databases + schemas they ultimately have access to. Then merge into a final
   table that is UNIQUE on (master_role, database, schema), with a JSON column
   that lists every child-role path that contributed access.

   DESIGN PRINCIPLES
   -----------------
   * Every stage writes to its own table — STG_01_..., STG_02_..., etc.
   * After every stage there is an INSPECT block (just SELECTs) that you can
     run by itself to verify the data before moving on.
   * Not optimised — readability > performance. Each stage does ONE thing.
   * Works on real ACCOUNT_USAGE data, but Section 0 lets you flip a switch
     and seed a small test dataset so anyone can exercise the pipeline.

   STAGES
   ------
     0   (optional) seed test data
     1   STG_01_MASTER_ROLES         — every FARMERS* role
     2   STG_02_ROLE_EDGES           — every parent->child role grant
     3   STG_03_ROLE_DB_GRANTS       — every role's DB/SCHEMA privileges
     4   STG_04_ROLE_PATHS           — recursive walk (master -> ... -> role)
     5   STG_05_MASTER_TO_DBSCHEMA   — explode paths to (master,role,db,sch,priv)
     6   FINAL_MASTER_ACCESS_MAP     — unique (master,db,sch) + JSON child map
   ========================================================================= */

USE ROLE       SECURITYADMIN;     -- needs ACCOUNT_USAGE access
USE WAREHOUSE  COMPUTE_WH;        -- adjust as required
CREATE DATABASE IF NOT EXISTS FARMERS_AUDIT;
CREATE SCHEMA   IF NOT EXISTS FARMERS_AUDIT.RBAC;
USE SCHEMA      FARMERS_AUDIT.RBAC;


/* =============================================================================
   SECTION 0  —  SOURCE SWITCH (test data vs. real ACCOUNT_USAGE)
   -----------------------------------------------------------------------------
   The pipeline reads from two views:  V_ROLE_EDGES  and  V_ROLE_OBJECT_GRANTS.
   Define them ONCE here, pointing at whichever source you want.
   ========================================================================= */

----- 0.A  TEST DATA (skip if running on real ACCOUNT_USAGE) ------------------
CREATE OR REPLACE TABLE STG_00_TEST_GRANTS (
    grantee_name  STRING,        -- the role receiving the grant
    granted_on    STRING,        -- 'ROLE' | 'DATABASE' | 'SCHEMA'
    privilege     STRING,        -- USAGE / SELECT / ...
    object_name   STRING,        -- name of role/db/schema
    db_name       STRING,
    schema_name   STRING
);

-- Sample hierarchy (mirrors the examples in the requirement)
--
--   FARMERS_MASTER1 -> CHILD1 -> CHILD2 -> CHILD3 (DB1.SCH1 USAGE,SELECT)
--                                       -> CHILD4 (DB2.SCH2 USAGE)
--                  -> CHILD10 -> CHILD5  (DB1.SCH1 USAGE)        <-- same db/sch
--                                       (DB3.SCH3 SELECT)
--   FARMERS_MASTER2 -> CHILD10 (shared with MASTER1)             <-- many-1
INSERT INTO STG_00_TEST_GRANTS VALUES
  -- role-to-role grants (parent gets USAGE on child)
  ('FARMERS_MASTER1','ROLE','USAGE','CHILD1'  ,NULL,NULL),
  ('FARMERS_MASTER1','ROLE','USAGE','CHILD10' ,NULL,NULL),
  ('FARMERS_MASTER2','ROLE','USAGE','CHILD10' ,NULL,NULL),
  ('CHILD1'         ,'ROLE','USAGE','CHILD2'  ,NULL,NULL),
  ('CHILD2'         ,'ROLE','USAGE','CHILD3'  ,NULL,NULL),
  ('CHILD2'         ,'ROLE','USAGE','CHILD4'  ,NULL,NULL),
  ('CHILD10'        ,'ROLE','USAGE','CHILD5'  ,NULL,NULL),
  -- role-to-db/schema grants
  ('CHILD3','SCHEMA','USAGE' ,'SCH1','DB1','SCH1'),
  ('CHILD3','SCHEMA','SELECT','SCH1','DB1','SCH1'),
  ('CHILD4','SCHEMA','USAGE' ,'SCH2','DB2','SCH2'),
  ('CHILD5','SCHEMA','USAGE' ,'SCH1','DB1','SCH1'),
  ('CHILD5','SCHEMA','SELECT','SCH3','DB3','SCH3');

----- 0.B  VIEWS THE PIPELINE READS FROM -------------------------------------
-- Pick ONE of the two CREATE VIEW blocks below.

-- ===> TEST-DATA mode: comment out the ACCOUNT_USAGE versions further down,
--                     and use these instead.
CREATE OR REPLACE VIEW V_ROLE_EDGES AS
SELECT grantee_name AS parent_role,
       object_name  AS child_role
FROM   STG_00_TEST_GRANTS
WHERE  granted_on = 'ROLE'
  AND  privilege  = 'USAGE';

CREATE OR REPLACE VIEW V_ROLE_OBJECT_GRANTS AS
SELECT grantee_name AS role_name,
       db_name      AS database_name,
       COALESCE(schema_name,'*') AS schema_name,
       privilege
FROM   STG_00_TEST_GRANTS
WHERE  granted_on IN ('DATABASE','SCHEMA');

-- ===> PRODUCTION mode: uncomment to read live grants from ACCOUNT_USAGE.
-- CREATE OR REPLACE VIEW V_ROLE_EDGES AS
-- SELECT GRANTEE_NAME AS parent_role, NAME AS child_role
-- FROM   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
-- WHERE  GRANTED_ON = 'ROLE' AND PRIVILEGE = 'USAGE' AND DELETED_ON IS NULL;
--
-- CREATE OR REPLACE VIEW V_ROLE_OBJECT_GRANTS AS
-- SELECT GRANTEE_NAME                       AS role_name,
--        TABLE_CATALOG                      AS database_name,
--        COALESCE(TABLE_SCHEMA,'*')         AS schema_name,
--        PRIVILEGE
-- FROM   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
-- WHERE  GRANTED_ON IN ('DATABASE','SCHEMA') AND DELETED_ON IS NULL;

-- INSPECT (Section 0)
SELECT * FROM V_ROLE_EDGES         ORDER BY parent_role, child_role;
SELECT * FROM V_ROLE_OBJECT_GRANTS ORDER BY role_name, database_name, schema_name;


/* =============================================================================
   STAGE 1  —  Capture every FARMERS* master role
   ========================================================================= */
CREATE OR REPLACE TABLE STG_01_MASTER_ROLES (
    master_role  STRING NOT NULL,
    load_ts      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- A master is any role that appears as a parent_role and starts with FARMERS.
-- (Switch to SNOWFLAKE.ACCOUNT_USAGE.ROLES in production if preferred.)
INSERT INTO STG_01_MASTER_ROLES (master_role)
SELECT DISTINCT parent_role
FROM   V_ROLE_EDGES
WHERE  parent_role ILIKE 'FARMERS%';

-- INSPECT
--   Q1: list every master role we will process.
SELECT * FROM STG_01_MASTER_ROLES ORDER BY master_role;
--   Q2: sanity check — is anything missing?
SELECT COUNT(*) AS master_count FROM STG_01_MASTER_ROLES;


/* =============================================================================
   STAGE 2  —  Materialise every parent->child role edge
   ========================================================================= */
CREATE OR REPLACE TABLE STG_02_ROLE_EDGES AS
SELECT parent_role,
       child_role
FROM   V_ROLE_EDGES;

-- INSPECT
--   Q1: full edge list — useful for drawing a graph on paper.
SELECT * FROM STG_02_ROLE_EDGES ORDER BY parent_role, child_role;
--   Q2: edges touching each master (1-1 / 1-many cases).
SELECT m.master_role, e.child_role
FROM   STG_01_MASTER_ROLES m
JOIN   STG_02_ROLE_EDGES   e ON e.parent_role = m.master_role
ORDER  BY m.master_role, e.child_role;
--   Q3: many-1 detection — any child with more than one parent?
SELECT child_role, COUNT(*) AS parent_count
FROM   STG_02_ROLE_EDGES
GROUP  BY child_role
HAVING COUNT(*) > 1
ORDER  BY parent_count DESC;


/* =============================================================================
   STAGE 3  —  Materialise every role's DB/SCHEMA grant
   ========================================================================= */
CREATE OR REPLACE TABLE STG_03_ROLE_DB_GRANTS AS
SELECT role_name,
       database_name,
       schema_name,
       privilege
FROM   V_ROLE_OBJECT_GRANTS;

-- INSPECT
--   Q1: every DB/SCHEMA grant, ordered.
SELECT * FROM STG_03_ROLE_DB_GRANTS
ORDER  BY role_name, database_name, schema_name, privilege;
--   Q2: which roles touch which databases?
SELECT role_name, database_name, ARRAY_AGG(DISTINCT schema_name) AS schemas
FROM   STG_03_ROLE_DB_GRANTS
GROUP  BY role_name, database_name
ORDER  BY role_name, database_name;


/* =============================================================================
   STAGE 4  —  Recursive walk: master -> child -> child -> ...
                              with the full path captured per row
   -----------------------------------------------------------------------------
   Output one row per (master_role, descendant_role) including a path array.
   ========================================================================= */
CREATE OR REPLACE TABLE STG_04_ROLE_PATHS (
    master_role     STRING,
    descendant_role STRING,
    depth           INT,
    role_path       ARRAY
);

INSERT INTO STG_04_ROLE_PATHS (master_role, descendant_role, depth, role_path)
WITH RECURSIVE walk AS (
    -- anchor: the master itself at depth 0
    SELECT  m.master_role                          AS master_role,
            m.master_role                          AS descendant_role,
            0                                      AS depth,
            ARRAY_CONSTRUCT(m.master_role)         AS role_path
    FROM    STG_01_MASTER_ROLES m

    UNION ALL

    -- recursion: each child of the current descendant becomes a new row
    SELECT  w.master_role,
            e.child_role,
            w.depth + 1,
            ARRAY_APPEND(w.role_path, e.child_role)
    FROM    walk w
    JOIN    STG_02_ROLE_EDGES e ON e.parent_role = w.descendant_role
    WHERE   ARRAY_POSITION(e.child_role::VARIANT, w.role_path) IS NULL  -- cycle guard
)
SELECT master_role, descendant_role, depth, role_path
FROM   walk;

-- INSPECT
--   Q1: every (master, descendant) with full path — eyeball the tree shape.
SELECT master_role, descendant_role, depth, role_path
FROM   STG_04_ROLE_PATHS
ORDER  BY master_role, depth, descendant_role;
--   Q2: count of descendants per master.
SELECT master_role, COUNT(*) AS descendant_count
FROM   STG_04_ROLE_PATHS
GROUP  BY master_role
ORDER  BY master_role;
--   Q3: deepest path per master.
SELECT master_role, MAX(depth) AS max_depth
FROM   STG_04_ROLE_PATHS
GROUP  BY master_role
ORDER  BY master_role;


/* =============================================================================
   STAGE 5  —  Join descendants to their DB/SCHEMA grants
   -----------------------------------------------------------------------------
   One row per (master_role, descendant_role, database, schema, privilege).
   This is the granular "fact" table — the merge in Stage 6 rolls it up.
   ========================================================================= */
CREATE OR REPLACE TABLE STG_05_MASTER_TO_DBSCHEMA AS
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

-- INSPECT
--   Q1: every granular access row.
SELECT * FROM STG_05_MASTER_TO_DBSCHEMA
ORDER  BY master_role, database_name, schema_name, depth, descendant_role;
--   Q2: how many (master, db, schema) combos do we have?
SELECT master_role, database_name, schema_name, COUNT(*) AS row_count
FROM   STG_05_MASTER_TO_DBSCHEMA
GROUP  BY 1,2,3
ORDER  BY 1,2,3;
--   Q3: spot the "two different paths reach the same DB/SCH" case.
SELECT master_role, database_name, schema_name,
       COUNT(DISTINCT descendant_role) AS distinct_paths
FROM   STG_05_MASTER_TO_DBSCHEMA
GROUP  BY 1,2,3
HAVING COUNT(DISTINCT descendant_role) > 1
ORDER  BY 1,2,3;


/* =============================================================================
   STAGE 6  —  Final merge: unique (master, db, schema) + JSON child map
   -----------------------------------------------------------------------------
   The JSON column captures every path that contributed access to this
   (master, db, schema). Schema is enforced via the primary key.
   ========================================================================= */
CREATE OR REPLACE TABLE FINAL_MASTER_ACCESS_MAP (
    master_role     STRING       NOT NULL,
    database_name   STRING       NOT NULL,
    schema_name     STRING       NOT NULL,
    permissions     ARRAY,                 -- distinct privileges across all paths
    child_map       VARIANT,               -- JSON: master + list of contributing paths
    load_ts         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_MASTER_DB_SCHEMA  PRIMARY KEY (master_role, database_name, schema_name)
);

-- Build one record per (master, db, schema) by aggregating Stage 5 twice:
--   inner aggregation: per (master,db,sch,descendant) collect privs+path
--   outer aggregation: per (master,db,sch) collect all those descendant records
WITH per_descendant AS (
    SELECT  master_role,
            database_name,
            schema_name,
            descendant_role,
            depth,
            role_path,
            ARRAY_AGG(DISTINCT privilege) WITHIN GROUP (ORDER BY privilege)
                                                            AS privs
    FROM    STG_05_MASTER_TO_DBSCHEMA
    GROUP   BY master_role, database_name, schema_name,
              descendant_role, depth, role_path
),
rolled AS (
    SELECT  master_role,
            database_name,
            schema_name,
            ARRAY_AGG(DISTINCT priv.value::STRING)
                WITHIN GROUP (ORDER BY priv.value::STRING)  AS permissions,
            OBJECT_CONSTRUCT(
                'master_role',  master_role,
                'database',     database_name,
                'schema',       schema_name,
                'paths',
                    ARRAY_AGG(
                        OBJECT_CONSTRUCT(
                            'descendant_role', descendant_role,
                            'depth',           depth,
                            'role_path',       role_path,
                            'privileges',      privs
                        )
                    ) WITHIN GROUP (ORDER BY depth, descendant_role)
            )                                              AS child_map
    FROM    per_descendant,
            LATERAL FLATTEN(input => privs) priv
    GROUP   BY master_role, database_name, schema_name
)
INSERT INTO FINAL_MASTER_ACCESS_MAP
       (master_role, database_name, schema_name, permissions, child_map)
SELECT  master_role, database_name, schema_name, permissions, child_map
FROM    rolled;

-- INSPECT
--   Q1: every final record (this is the deliverable).
SELECT master_role, database_name, schema_name, permissions, child_map
FROM   FINAL_MASTER_ACCESS_MAP
ORDER  BY master_role, database_name, schema_name;
--   Q2: uniqueness check — MUST return 0 rows.
SELECT master_role, database_name, schema_name, COUNT(*) AS dup
FROM   FINAL_MASTER_ACCESS_MAP
GROUP  BY 1,2,3
HAVING COUNT(*) > 1;
--   Q3: pretty-print one child_map JSON.
SELECT master_role, database_name, schema_name,
       child_map:paths AS paths
FROM   FINAL_MASTER_ACCESS_MAP
WHERE  master_role = 'FARMERS_MASTER1'
  AND  database_name = 'DB1'
  AND  schema_name  = 'SCH1';
--   Q4: flatten the JSON back out to columns for cross-checking with Stage 5.
SELECT  m.master_role,
        m.database_name,
        m.schema_name,
        p.value:descendant_role::STRING AS descendant_role,
        p.value:depth::INT              AS depth,
        p.value:role_path               AS role_path,
        p.value:privileges              AS privileges
FROM    FINAL_MASTER_ACCESS_MAP m,
        LATERAL FLATTEN(input => m.child_map:paths) p
ORDER   BY m.master_role, m.database_name, m.schema_name, depth, descendant_role;


/* =============================================================================
   EXPECTED RESULT (using the test data in Section 0.A)
   -----------------------------------------------------------------------------
   FINAL_MASTER_ACCESS_MAP should have 4 rows:

     FARMERS_MASTER1 | DB1 | SCH1 | [USAGE,SELECT]
        paths: CHILD3 via [FARMERS_MASTER1,CHILD1,CHILD2,CHILD3]   (USAGE,SELECT)
               CHILD5 via [FARMERS_MASTER1,CHILD10,CHILD5]         (USAGE)
     FARMERS_MASTER1 | DB2 | SCH2 | [USAGE]
        paths: CHILD4 via [FARMERS_MASTER1,CHILD1,CHILD2,CHILD4]   (USAGE)
     FARMERS_MASTER1 | DB3 | SCH3 | [SELECT]
        paths: CHILD5 via [FARMERS_MASTER1,CHILD10,CHILD5]         (SELECT)
     FARMERS_MASTER2 | DB1 | SCH1 | [USAGE]
        paths: CHILD5 via [FARMERS_MASTER2,CHILD10,CHILD5]         (USAGE)
        ... plus DB3.SCH3 and any others inherited through CHILD10.

   If you see exactly these rows, the pipeline is wired up correctly.
   ========================================================================= */
