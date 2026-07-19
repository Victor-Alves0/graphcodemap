------------------------------------------------------------
-- SCHEMA
------------------------------------------------------------

CREATE SCHEMA company;

SET search_path TO company;

------------------------------------------------------------
-- TABLES
------------------------------------------------------------

CREATE TABLE departments
(
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE employees
(
    id SERIAL PRIMARY KEY,

    department_id INTEGER
        REFERENCES departments(id),

    name TEXT NOT NULL,

    salary NUMERIC(10,2)
        CHECK(salary > 0),

    manager_id INTEGER
        REFERENCES employees(id),

    metadata JSONB,

    created_at TIMESTAMP
        DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE projects
(
    id SERIAL PRIMARY KEY,

    name TEXT,

    budget NUMERIC,

    active BOOLEAN DEFAULT TRUE
);

CREATE TABLE employee_project
(
    employee_id INTEGER
        REFERENCES employees(id),

    project_id INTEGER
        REFERENCES projects(id),

    hours INTEGER,

    PRIMARY KEY(
        employee_id,
        project_id
    )
);

------------------------------------------------------------
-- INDEXES
------------------------------------------------------------

CREATE INDEX idx_employee_name
ON employees(name);

CREATE INDEX idx_project_budget
ON projects(budget);

------------------------------------------------------------
-- VIEW
------------------------------------------------------------

CREATE VIEW employee_summary AS

SELECT

    e.id,

    e.name,

    d.name AS department,

    e.salary

FROM employees e

JOIN departments d

ON e.department_id=d.id;

------------------------------------------------------------
-- MATERIALIZED VIEW
------------------------------------------------------------

CREATE MATERIALIZED VIEW salary_stats AS

SELECT

    department_id,

    AVG(salary) avg_salary,

    MAX(salary) max_salary,

    MIN(salary) min_salary

FROM employees

GROUP BY department_id;

------------------------------------------------------------
-- FUNCTION
------------------------------------------------------------

CREATE OR REPLACE FUNCTION
bonus(value NUMERIC)

RETURNS NUMERIC

LANGUAGE plpgsql

AS
$$

BEGIN

    RETURN value*0.10;

END;

$$;

------------------------------------------------------------
-- PROCEDURE
------------------------------------------------------------

CREATE OR REPLACE PROCEDURE
increase_salary(percent NUMERIC)

LANGUAGE plpgsql

AS
$$

BEGIN

    UPDATE employees

    SET salary=
        salary+
        salary*percent;

END;

$$;

------------------------------------------------------------
-- TRIGGER
------------------------------------------------------------

CREATE TABLE audit_log
(
    id SERIAL PRIMARY KEY,

    employee_id INTEGER,

    action TEXT,

    created_at TIMESTAMP
);

CREATE OR REPLACE FUNCTION
employee_audit()

RETURNS TRIGGER

LANGUAGE plpgsql

AS
$$

BEGIN

    INSERT INTO audit_log(

        employee_id,

        action,

        created_at

    )

    VALUES(

        NEW.id,

        'INSERT',

        NOW()

    );

    RETURN NEW;

END;

$$;

CREATE TRIGGER trg_employee

AFTER INSERT

ON employees

FOR EACH ROW

EXECUTE FUNCTION employee_audit();

------------------------------------------------------------
-- INSERTS
------------------------------------------------------------

INSERT INTO departments(name)

VALUES

('Engineering'),
('Finance'),
('Sales');

INSERT INTO employees(

department_id,
name,
salary

)

VALUES

(1,'Alice',5000),

(1,'Bob',7000),

(2,'Carol',4000),

(3,'David',3000);

------------------------------------------------------------
-- CTE
------------------------------------------------------------

WITH salaries AS

(

SELECT

salary

FROM employees

)

SELECT

AVG(salary)

FROM salaries;

------------------------------------------------------------
-- RECURSIVE CTE
------------------------------------------------------------

WITH RECURSIVE hierarchy AS

(

SELECT

id,

manager_id,

name

FROM employees

WHERE manager_id IS NULL

UNION ALL

SELECT

e.id,

e.manager_id,

e.name

FROM employees e

JOIN hierarchy h

ON h.id=e.manager_id

)

SELECT *

FROM hierarchy;

------------------------------------------------------------
-- WINDOW
------------------------------------------------------------

SELECT

name,

salary,

RANK()

OVER(

ORDER BY salary DESC

)

FROM employees;

------------------------------------------------------------
-- JSON
------------------------------------------------------------

UPDATE employees

SET metadata='{

"skills":["SQL","Python"]

}'::jsonb

WHERE id=1;

------------------------------------------------------------
-- ARRAY
------------------------------------------------------------

SELECT

ARRAY_AGG(name)

FROM employees;

------------------------------------------------------------
-- EXISTS
------------------------------------------------------------

SELECT *

FROM departments d

WHERE EXISTS(

SELECT 1

FROM employees e

WHERE e.department_id=d.id

);

------------------------------------------------------------
-- HAVING
------------------------------------------------------------

SELECT

department_id,

COUNT(*)

FROM employees

GROUP BY department_id

HAVING COUNT(*)>1;

------------------------------------------------------------
-- CASE
------------------------------------------------------------

SELECT

name,

CASE

WHEN salary>6000
THEN 'HIGH'

WHEN salary>4000
THEN 'MEDIUM'

ELSE 'LOW'

END

FROM employees;

------------------------------------------------------------
-- UNION
------------------------------------------------------------

SELECT name FROM employees

UNION

SELECT name FROM departments;

------------------------------------------------------------
-- INTERSECT
------------------------------------------------------------

SELECT id

FROM employees

INTERSECT

SELECT employee_id

FROM employee_project;

------------------------------------------------------------
-- EXCEPT
------------------------------------------------------------

SELECT id

FROM employees

EXCEPT

SELECT employee_id

FROM employee_project;

------------------------------------------------------------
-- TRANSACTION
------------------------------------------------------------

BEGIN;

CALL increase_salary(0.05);

COMMIT;

------------------------------------------------------------
-- FUNCTION CALL
------------------------------------------------------------

SELECT

name,

bonus(salary)

FROM employees;

------------------------------------------------------------
-- COMPLEX QUERY
------------------------------------------------------------

SELECT

e.name,

d.name,

SUM(ep.hours),

AVG(e.salary),

COUNT(*) OVER()

FROM employees e

JOIN departments d

ON d.id=e.department_id

LEFT JOIN employee_project ep

ON ep.employee_id=e.id

GROUP BY

e.name,

d.name;