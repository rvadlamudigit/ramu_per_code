/* =============================================================================
   Snowflake DML — Simple loop over every FRMS* master role.
   For every master:
       1. Find ALL descendant roles (any depth)
       2. For each (descendant, database, schema) compute its permissions
       3. Insert one row into the target table with the role tree JSON
   ========================================================================= */

USE ROLE       SECURITYADMIN;     -- needs ACCOUNT_USAGE access
USE WAREHOUSE  COMPUTE_WH;        -- adjust if needed

------------------------------------------------------------------------------
-- 1. Target table
------------------------------------------------------------------------------
CREATE OR REPLACE TABLE role_audit_output (
    parentrole  STRING,        -- the FRMS* master role
    childrole   STRING,        -- a descendant role somewhere under it
    role_json   VARIANT,       -- { role_tree, permissions, database, schema }
    load_ts     TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

------------------------------------------------------------------------------
-- 2. Simple procedural block
--    - outer cursor: every role whose name starts with FRMS
--    - inner work:   recursive walk + grant resolution, inserted in one DML
------------------------------------------------------------------------------
DECLARE
    master_cur CURSOR FOR
        SELECT DISTINCT NAME AS master_role
        FROM   SNOWFLAKE.ACCOUNT_USAGE.ROLES
        WHERE  NAME ILIKE 'FRMS%'
          AND  DELETED_ON IS NULL
        ORDER BY NAME;

    v_master  STRING;
    v_count   INTEGER DEFAULT 0;
BEGIN
    --------------------------------------------------------------------------
    -- Loop until every FRMS* role has been processed
    --------------------------------------------------------------------------
    FOR rec IN master_cur DO

        v_master := rec.master_role;

        ----------------------------------------------------------------------
        -- One INSERT per master:
        --   walk     = recursive descent from master through every child
        --   grants   = each role's DB/SCHEMA privileges
        --   one row per (master, descendant, db, schema)
        ----------------------------------------------------------------------
        INSERT INTO role_audit_output (parentrole, childrole, role_json)
        WITH RECURSIVE walk AS (
            -- anchor: the master itself
            SELECT  :v_master                        AS role_name,
                    0                                AS lvl,
                    ARRAY_CONSTRUCT(:v_master)       AS role_tree

            UNION ALL

            -- recurse through every USAGE-on-ROLE grant
            SELECT  g.NAME,
                    w.lvl + 1,
                    ARRAY_APPEND(w.role_tree, g.NAME)
            FROM    walk w
            JOIN    SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES g
                   ON g.GRANTEE_NAME = w.role_name
            WHERE   g.GRANTED_ON  = 'ROLE'
              AND   g.PRIVILEGE   = 'USAGE'
              AND   g.DELETED_ON IS NULL
              AND   ARRAY_POSITION(g.NAME::VARIANT, w.role_tree) IS NULL   -- cycle guard
        )
        SELECT
            :v_master                                                AS parentrole,
            w.role_name                                              AS childrole,
            OBJECT_CONSTRUCT(
                'role_tree',   w.role_tree,
                'level',       w.lvl,
                'database',    g.TABLE_CATALOG,
                'schema',      COALESCE(g.TABLE_SCHEMA, '*'),
                'permissions', ARRAY_AGG(DISTINCT g.PRIVILEGE)
                                  WITHIN GROUP (ORDER BY g.PRIVILEGE)
            )                                                        AS role_json
        FROM   walk w
        LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES g
               ON g.GRANTEE_NAME = w.role_name
              AND g.GRANTED_ON  IN ('DATABASE','SCHEMA')
              AND g.DELETED_ON IS NULL
        GROUP BY w.role_name, w.lvl, w.role_tree,
                 g.TABLE_CATALOG, g.TABLE_SCHEMA;

        v_count := v_count + 1;

    END FOR;
    --------------------------------------------------------------------------
    -- Done — return how many master roles we processed
    --------------------------------------------------------------------------
    RETURN 'Processed ' || v_count || ' FRMS* master role(s).';
END;

------------------------------------------------------------------------------
-- 3. Verification
------------------------------------------------------------------------------
-- 3a. Row count
SELECT COUNT(*) AS rows_loaded FROM role_audit_output;

-- 3b. Preview
SELECT parentrole, childrole, role_json
FROM   role_audit_output
ORDER  BY parentrole, role_json:level, childrole;

-- 3c. Pull out the JSON pieces as columns (handy for review)
SELECT  parentrole,
        childrole,
        role_json:database::STRING        AS database_name,
        role_json:schema::STRING          AS schema_name,
        role_json:permissions             AS permissions,
        role_json:role_tree               AS role_tree,
        role_json:level::INT              AS depth_from_master
FROM    role_audit_output
ORDER   BY parentrole, depth_from_master, childrole;
