/* =============================================================================
   FARMERS — Role / Warehouse Lookup Queries
   -----------------------------------------------------------------------------
   Four ready-to-run queries for finding what's "assigned" to a master role:

      Q1. Warehouses granted DIRECTLY to the master role
      Q2. Warehouses INHERITED through child-role chains (recursive)
      Q3. Child roles directly granted to the master
      Q4. Users assigned to the master + their default warehouse/role

   Source: SNOWFLAKE.ACCOUNT_USAGE  (up to ~2 hours of latency)
   ========================================================================= */

USE ROLE      SECURITYADMIN;
USE WAREHOUSE COMPUTE_WH;

-- Replace this in each query with the master role you want to inspect:
--   :master_role   ->   'FARMERS_MASTER1'

/* =============================================================================
   Q1.  Warehouses granted DIRECTLY to a master role
   ========================================================================= */
SELECT  GRANTEE_NAME          AS master_role,
        NAME                  AS warehouse_name,
        PRIVILEGE,                                   -- USAGE / OPERATE / MONITOR / ...
        GRANTED_BY,
        CREATED_ON
FROM    SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
WHERE   GRANTED_ON   = 'WAREHOUSE'
  AND   GRANTEE_NAME = 'FARMERS_MASTER1'             -- <-- change me
  AND   DELETED_ON IS NULL
ORDER BY warehouse_name, PRIVILEGE;


/* =============================================================================
   Q2.  Warehouses INHERITED — recursive walk from master through every child
        Returns the via_role, full role_path, and the warehouse + privilege.
   ========================================================================= */
WITH RECURSIVE walk AS (
    SELECT  'FARMERS_MASTER1'                              AS master_role,   -- <-- change me
            'FARMERS_MASTER1'                              AS role_name,
            0                                              AS depth,
            ARRAY_CONSTRUCT('FARMERS_MASTER1')             AS role_path

    UNION ALL

    SELECT  w.master_role,
            g.NAME,
            w.depth + 1,
            ARRAY_APPEND(w.role_path, g.NAME)
    FROM    walk w
    JOIN    SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES g
           ON g.GRANTEE_NAME = w.role_name
    WHERE   g.GRANTED_ON  = 'ROLE'
      AND   g.PRIVILEGE   = 'USAGE'
      AND   g.DELETED_ON IS NULL
      AND   ARRAY_POSITION(g.NAME::VARIANT, w.role_path) IS NULL  -- cycle guard
)
SELECT  w.master_role,
        w.role_name        AS via_role,
        w.depth,
        w.role_path,
        wh.NAME            AS warehouse_name,
        wh.PRIVILEGE       AS warehouse_privilege,
        wh.GRANTED_BY,
        wh.CREATED_ON
FROM    walk w
JOIN    SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES wh
       ON wh.GRANTEE_NAME = w.role_name
WHERE   wh.GRANTED_ON  = 'WAREHOUSE'
  AND   wh.DELETED_ON IS NULL
ORDER BY w.master_role, wh.NAME, w.depth, w.role_name;


/* =============================================================================
   Q3.  Child roles directly granted to a master role
   ========================================================================= */
SELECT  GRANTEE_NAME    AS master_role,
        NAME            AS child_role,
        PRIVILEGE,
        GRANTED_BY,
        CREATED_ON
FROM    SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
WHERE   GRANTED_ON   = 'ROLE'
  AND   PRIVILEGE    = 'USAGE'
  AND   GRANTEE_NAME = 'FARMERS_MASTER1'             -- <-- change me
  AND   DELETED_ON IS NULL
ORDER BY child_role;


/* =============================================================================
   Q4.  Users assigned to the master role + their default warehouse / role
   ========================================================================= */
SELECT  g.GRANTEE_NAME        AS user_name,
        g.ROLE                AS master_role,
        u.DEFAULT_WAREHOUSE,
        u.DEFAULT_ROLE,
        u.DEFAULT_NAMESPACE,
        u.DISABLED,
        u.LAST_SUCCESS_LOGIN
FROM    SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
JOIN    SNOWFLAKE.ACCOUNT_USAGE.USERS u
       ON u.NAME = g.GRANTEE_NAME
WHERE   g.ROLE        = 'FARMERS_MASTER1'            -- <-- change me
  AND   g.DELETED_ON IS NULL
  AND   u.DELETED_ON IS NULL
ORDER BY user_name;


/* =============================================================================
   Q5.  Convenience — all warehouses + child roles reachable from EVERY
        FARMERS* master, in one shot.
   ========================================================================= */
WITH RECURSIVE masters AS (
    SELECT DISTINCT GRANTEE_NAME AS master_role
    FROM   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES
    WHERE  GRANTED_ON = 'ROLE'
      AND  PRIVILEGE  = 'USAGE'
      AND  DELETED_ON IS NULL
      AND  GRANTEE_NAME ILIKE 'FARMERS%'
),
walk AS (
    SELECT m.master_role, m.master_role AS role_name, 0 AS depth,
           ARRAY_CONSTRUCT(m.master_role) AS role_path
    FROM   masters m
    UNION ALL
    SELECT w.master_role, g.NAME, w.depth + 1,
           ARRAY_APPEND(w.role_path, g.NAME)
    FROM   walk w
    JOIN   SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES g
          ON g.GRANTEE_NAME = w.role_name
    WHERE  g.GRANTED_ON = 'ROLE'
      AND  g.PRIVILEGE  = 'USAGE'
      AND  g.DELETED_ON IS NULL
      AND  ARRAY_POSITION(g.NAME::VARIANT, w.role_path) IS NULL
)
SELECT  w.master_role,
        wh.NAME                AS warehouse_name,
        ARRAY_AGG(DISTINCT wh.PRIVILEGE) WITHIN GROUP (ORDER BY wh.PRIVILEGE) AS privileges,
        ARRAY_AGG(DISTINCT w.role_name)  WITHIN GROUP (ORDER BY w.role_name)  AS via_roles
FROM    walk w
JOIN    SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES wh
       ON wh.GRANTEE_NAME = w.role_name
WHERE   wh.GRANTED_ON  = 'WAREHOUSE'
  AND   wh.DELETED_ON IS NULL
GROUP BY w.master_role, wh.NAME
ORDER BY w.master_role, wh.NAME;
