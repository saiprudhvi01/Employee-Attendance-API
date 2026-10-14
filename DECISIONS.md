# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Created unique index on employees.emp_code for uniqueness guarantee, compound index on (department, emp_code) for sorted department queries, unique (emp_code, date) on attendance_logs for punch-in race safety, (date, emp_code) for attendance list sorting, (status, date, emp_code) for status-filtered queries, and (emp_code, punch_in) for punch-out lookups. Considered an index on joined_on alone but rejected it since the department summary already filters by joined_on in the match stage before grouping.

2. **Punch-in race.** Both requests try insert_one simultaneously. The unique index on (emp_code, date) ensures only one succeeds. The winner gets 201, the loser catches DuplicateKeyError and returns 409. No check-then-insert race condition exists.

3. **Ties.** Returns all employees with rank <= limit because the cutoff applies to rank, not row count. Using $rank with standard competition ranking means tied employees share the same rank, so if three employees tie at rank 5 and limit=5, all three are returned.

4. **Headcount.** Starts aggregation from employees collection, not attendance_logs. Uses $lookup with preserveNullAndEmptyArrays=True so employees with no matching logs still appear in the pipeline, then counts them in the $group stage.

5. **One thing you would change** if this had to serve 100x the data. Pre-aggregate monthly rollups in a separate collection updated daily via background jobs, so analytics queries read pre-computed summaries instead of aggregating raw logs every time.
