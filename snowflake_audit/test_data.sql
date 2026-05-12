-- =============================================================================
-- test_data.sql
--
-- Offline test harness for role_access_map.sql. Drops a fake
-- TEST_GRANTS_TO_ROLES table populated with the example scenario:
--
--     db1/schema -> role1 -> MasterRole1
--     db2/schema -> role2 -> MasterRole1
--     db1        -> role3 -> role1 -> MasterRole1
--     db1        -> role3 -> MasterRole2
--
-- To use:
--   1. Run this file once to create TEST_GRANTS_TO_ROLES.
--   2. Run a copy of role_access_map.sql with
--      `SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES` replaced with
--      `TEST_GRANTS_TO_ROLES` (both occurrences).
--   3. SELECT * FROM ROLE_ACCESS_MAP  to inspect the result.
--
-- Expected rows (chain shown abbreviated):
--   MASTERROLE1 | [MASTERROLE1, ROLE1]              | DB1 | SCHEMA
--   MASTERROLE1 | [MASTERROLE1, ROLE2]              | DB2 | SCHEMA
--   MASTERROLE1 | [MASTERROLE1, ROLE1, ROLE3]       | DB1 | (null)
--   MASTERROLE2 | [MASTERROLE2, ROLE3]              | DB1 | (null)
-- =============================================================================

CREATE OR REPLACE TABLE TEST_GRANTS_TO_ROLES (
    DELETED_ON   TIMESTAMP_LTZ,
    PRIVILEGE    VARCHAR,
    GRANTED_ON   VARCHAR,
    NAME         VARCHAR,
    GRANTED_TO   VARCHAR,
    GRANTEE_NAME VARCHAR
);

INSERT INTO TEST_GRANTS_TO_ROLES
    (DELETED_ON, PRIVILEGE, GRANTED_ON, NAME, GRANTED_TO, GRANTEE_NAME)
VALUES
    -- ---- role -> role inheritance ----
    -- ( PARENT = GRANTEE_NAME inherits CHILD = NAME )
    (NULL, 'USAGE', 'ROLE',     'ROLE1',      'ROLE', 'MASTERROLE1'),
    (NULL, 'USAGE', 'ROLE',     'ROLE2',      'ROLE', 'MASTERROLE1'),
    (NULL, 'USAGE', 'ROLE',     'ROLE3',      'ROLE', 'ROLE1'),
    (NULL, 'USAGE', 'ROLE',     'ROLE3',      'ROLE', 'MASTERROLE2'),

    -- ---- direct object grants ----
    (NULL, 'USAGE', 'SCHEMA',   'DB1.SCHEMA', 'ROLE', 'ROLE1'),
    (NULL, 'USAGE', 'SCHEMA',   'DB2.SCHEMA', 'ROLE', 'ROLE2'),
    (NULL, 'USAGE', 'DATABASE', 'DB1',        'ROLE', 'ROLE3');


-- ---------------------- quick sanity check ----------------------
-- SELECT GRANTED_ON, NAME, GRANTEE_NAME
-- FROM   TEST_GRANTS_TO_ROLES
-- ORDER  BY GRANTED_ON, GRANTEE_NAME;
