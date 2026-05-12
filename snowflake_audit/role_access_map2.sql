/* =============================================================================
   Snowflake DML — Build Parent / Leaf-Child Map + Nested Tree JSON
   -----------------------------------------------------------------------------
   Source rows
        parentrole | childrole
        -----------+----------
        frms1      | b
        b          | d
        frms1      | c
        c          | d
        c          | e

   Target #1 (flat)
        parent | child_map
        -------+-----------
        frms1  | d
        frms1  | e

   Target #2 (nested JSON / dict) — one tree per master role
        {
          "parent": "frms1",
          "children": [
            { "role": "b", "children": [ { "role": "d" } ] },
            { "role": "c", "children": [ { "role": "d" }, { "role": "e" } ] }
          ]
        }
   ========================================================================= */

------------------------------------------------------------------------------
-- 0. (Re)build the source table and load the sample edges
------------------------------------------------------------------------------
CREATE OR REPLACE TABLE role_hierarchy_src (
    parentrole STRING,
    childrole  STRING
);

INSERT INTO role_hierarchy_src (parentrole, childrole) VALUES
    ('frms1', 'b'),
    ('b',     'd'),
    ('frms1', 'c'),
    ('c',     'd'),
    ('c',     'e');

------------------------------------------------------------------------------
-- 1. Target table — one row per (master_role, leaf_descendant)
------------------------------------------------------------------------------
CREATE OR REPLACE TABLE role_hierarchy_target (
    parent     STRING,
    child_map  STRING,           -- leaf descendant
    tree       VARIANT,          -- nested JSON dict for the whole subtree
    LOAD_TS    TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_PARENT_LEAF PRIMARY KEY (parent, child_map)
);

------------------------------------------------------------------------------
-- 2. Recursive walk — every master role to every descendant + full path
--    Master = any parentrole that never appears as a childrole.
------------------------------------------------------------------------------
WITH RECURSIVE
masters AS (
    SELECT DISTINCT parentrole AS master_role
    FROM   role_hierarchy_src s
    WHERE  NOT EXISTS (
              SELECT 1
              FROM   role_hierarchy_src x
              WHERE  x.childrole = s.parentrole
           )
),
walk AS (
    /* anchor — start at every master */
    SELECT  m.master_role,
            m.master_role            AS role_name,
            0                        AS lvl,
            ARRAY_CONSTRUCT(m.master_role) AS path
    FROM    masters m

    UNION ALL

    /* recurse through edges */
    SELECT  w.master_role,
            s.childrole,
            w.lvl + 1,
            ARRAY_APPEND(w.path, s.childrole)
    FROM    walk w
    JOIN    role_hierarchy_src s
           ON s.parentrole = w.role_name
    /* cycle guard */
    WHERE   ARRAY_POSITION(s.childrole::VARIANT, w.path) IS NULL
),
leaves AS (
    /* a node is a leaf if it has no outgoing edge */
    SELECT  DISTINCT
            master_role AS parent,
            role_name   AS child_map
    FROM    walk w
    WHERE   role_name <> master_role
      AND   NOT EXISTS (
                SELECT 1
                FROM   role_hierarchy_src s
                WHERE  s.parentrole = w.role_name
            )
)
------------------------------------------------------------------------------
-- 3. Upsert the flat (parent, leaf) rows
------------------------------------------------------------------------------
MERGE INTO role_hierarchy_target tgt
USING leaves src
   ON tgt.parent = src.parent AND tgt.child_map = src.child_map
WHEN NOT MATCHED THEN INSERT (parent, child_map)
                       VALUES (src.parent, src.child_map);

------------------------------------------------------------------------------
-- 4. Build the nested JSON tree per master role with a JavaScript SP.
--    Pure SQL can't easily aggregate bottom-up into nested objects, so we
--    do the recursion in JS once and write the dict back to the target table.
------------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE build_role_tree_json()
RETURNS STRING
LANGUAGE JAVASCRIPT
EXECUTE AS CALLER
AS
$$
    // ---- 1. Pull all edges into memory --------------------------------------
    var edges     = {};   // parent  -> Set(children)
    var parents   = {};   // child   -> Set(parents)   (used to find roots)
    var rs = snowflake.execute({
        sqlText: "SELECT parentrole, childrole FROM role_hierarchy_src"
    });
    while (rs.next()) {
        var p = rs.getColumnValue(1);
        var c = rs.getColumnValue(2);
        if (!edges[p])   edges[p]   = {};
        if (!parents[c]) parents[c] = {};
        edges[p][c]   = true;
        parents[c][p] = true;
    }

    // ---- 2. Identify masters (parents that are never a child) ---------------
    var masters = [];
    for (var p in edges) if (!parents[p]) masters.push(p);

    // ---- 3. Recursively build a nested dict per master ----------------------
    function build(role, ancestors) {
        var node = { role: role };
        var kids = edges[role] ? Object.keys(edges[role]) : [];
        if (kids.length) {
            node.children = [];
            for (var i = 0; i < kids.length; i++) {
                var c = kids[i];
                if (ancestors[c]) continue;          // cycle guard
                var nextAnc = Object.assign({}, ancestors);
                nextAnc[c] = true;
                node.children.push(build(c, nextAnc));
            }
        }
        return node;
    }

    // ---- 4. Persist one tree per master to the target table -----------------
    for (var i = 0; i < masters.length; i++) {
        var m    = masters[i];
        var tree = { parent: m, children: (build(m, {[m]:true}).children || []) };
        snowflake.execute({
            sqlText: "UPDATE role_hierarchy_target " +
                     "   SET tree = PARSE_JSON(?) " +
                     " WHERE parent = ?",
            binds: [ JSON.stringify(tree), m ]
        });
    }

    return "OK — " + masters.length + " master tree(s) written.";
$$;

CALL build_role_tree_json();

------------------------------------------------------------------------------
-- 5. Verification
------------------------------------------------------------------------------
-- 5a. Flat target rows
SELECT parent, child_map
FROM   role_hierarchy_target
ORDER  BY parent, child_map;

-- 5b. The nested JSON for each master (one row per master)
SELECT DISTINCT parent, tree
FROM   role_hierarchy_target
ORDER  BY parent;

-- 5c. Uniqueness check  (must return 0)
SELECT parent, child_map, COUNT(*) dup
FROM   role_hierarchy_target
GROUP  BY 1,2
HAVING COUNT(*) > 1;

