-- Data consistency audit for access, GPU scheduling contexts and the scheduler tables.
-- Every row should show 0 problems. Run:
--   docker exec -i prayog-postgres psql -U prayog -d <database> < backend/scripts/audit_consistency.sql
SELECT check_name, problems, CASE WHEN problems = 0 THEN 'ok' ELSE 'CHECK' END AS result FROM (
  -- access and roles
  SELECT '01 approved request has no student/researcher row' AS check_name, count(*) AS problems
    FROM access_requests ar
   WHERE ar.final_status = 'APPROVED'
     AND NOT EXISTS (SELECT 1 FROM students s WHERE s.user_id = ar.user_id)
     AND NOT EXISTS (SELECT 1 FROM researchers r WHERE r.user_id = ar.user_id)
  UNION ALL
  SELECT '02 student/researcher row without an approved request', count(*) FROM (
    SELECT user_id FROM students UNION ALL SELECT user_id FROM researchers) x
   WHERE NOT EXISTS (SELECT 1 FROM access_requests ar WHERE ar.user_id = x.user_id AND ar.final_status = 'APPROVED')
  UNION ALL
  SELECT '03 approved request has no scheduling context', count(*)
    FROM access_requests ar
   WHERE ar.final_status = 'APPROVED'
     AND NOT EXISTS (SELECT 1 FROM scheduling_contexts sc WHERE sc.user_id = ar.user_id)
  UNION ALL
  SELECT '04 scheduling context without an approved request', count(*)
    FROM scheduling_contexts sc
   WHERE NOT EXISTS (SELECT 1 FROM access_requests ar WHERE ar.user_id = sc.user_id AND ar.final_status = 'APPROVED')
  UNION ALL
  SELECT '05 context account type disagrees with the approved purpose', count(*)
    FROM scheduling_contexts sc JOIN access_requests ar ON ar.user_id = sc.user_id AND ar.final_status = 'APPROVED'
   WHERE sc.account_type <> CASE ar.purpose WHEN 'COURSE_PROJECT' THEN 'course' ELSE 'research' END
  UNION ALL
  SELECT '06 course context whose lab is not the central lab', count(*)
    FROM scheduling_contexts sc JOIN labs l ON l.id = sc.lab_id
   WHERE sc.account_type = 'course' AND NOT l.is_central_owner
       AND sc.course_cap_gpu_hours IS NULL
  UNION ALL
  SELECT '07 research context in a lab other than the professor''s', count(*)
    FROM scheduling_contexts sc
    JOIN access_requests ar ON ar.user_id = sc.user_id AND ar.final_status = 'APPROVED'
    JOIN professors p ON p.id = ar.professor_id
   WHERE sc.account_type = 'research' AND sc.lab_id IS DISTINCT FROM p.lab_id
  UNION ALL
  SELECT '08 students past their end date still approved (needs the scheduler tick running)', count(*)
    FROM access_requests WHERE final_status = 'APPROVED' AND role = 'STUDENT' AND end_date <= (now() AT TIME ZONE 'utc')
  UNION ALL
  SELECT '09 user with more than one open request (pending/rejected/approved)', count(*) FROM (
    SELECT user_id FROM access_requests WHERE final_status IN ('PENDING','REJECTED','APPROVED')
     GROUP BY user_id HAVING count(*) > 1) x
  UNION ALL
  SELECT '10 revoked request without a revoked_at/reason', count(*)
    FROM access_requests WHERE final_status = 'REVOKED' AND (revoked_at IS NULL OR revoke_reason IS NULL)
  UNION ALL
  SELECT '11 more than one central lab', GREATEST(count(*) - 1, 0) FROM labs WHERE is_central_owner
  -- scheduler tables
  UNION ALL
  SELECT '12 SCHEDULED/RUNNING job without a live allocation', count(*)
    FROM jobs j WHERE j.state IN ('SCHEDULED','RUNNING')
     AND NOT EXISTS (SELECT 1 FROM allocations a WHERE a.job_id = j.id AND a.released_at IS NULL)
  UNION ALL
  SELECT '13 live allocation whose job is not SCHEDULED/RUNNING', count(*)
    FROM allocations a JOIN jobs j ON j.id = a.job_id
   WHERE a.released_at IS NULL AND j.state NOT IN ('SCHEDULED','RUNNING')
  UNION ALL
  SELECT '14 GPU unit held by a released allocation', count(*)
    FROM gpu_units u JOIN allocations a ON a.id = u.current_allocation_id WHERE a.released_at IS NOT NULL
  UNION ALL
  SELECT '15 live allocation on a node of the wrong pool/lab for its job', count(*)
    FROM allocations a JOIN jobs j ON j.id = a.job_id JOIN nodes n ON n.id = a.node_id
   WHERE a.released_at IS NULL
     AND ((j.account_type = 'course') <> n.teaching_pool
          OR (n.donation_mode = 'reserved' AND n.owner_lab_id <> j.lab_id))
  UNION ALL
  SELECT '16 node whose live unit count differs from unit_count', count(*) FROM (
    SELECT n.id FROM nodes n LEFT JOIN gpu_units u ON u.node_id = n.id AND u.retired_at IS NULL
     WHERE n.state <> 'DECOMMISSIONED' GROUP BY n.id, n.unit_count HAVING count(u.id) <> n.unit_count) x
  UNION ALL
  SELECT '17 usage entry whose consumer lab differs from its job''s lab', count(*)
    FROM usage_entries ue JOIN jobs j ON j.id = ue.job_id WHERE ue.consumer_lab_id <> j.lab_id
) audit ORDER BY check_name;
