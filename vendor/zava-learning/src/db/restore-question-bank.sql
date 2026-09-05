TRUNCATE question_bank RESTART IDENTITY;

INSERT INTO question_bank (course_id, prompt, options, answer_idx, active)
SELECT
  (ARRAY['BIO-101','MATH-220','HIST-180','CHEM-110','ECON-201','PSY-100'])[1 + (g % 6)],
  'Practice question #' || g,
  '["A","B","C","D"]'::jsonb,
  (g % 4),
  true
FROM generate_series(1, 500000) AS g;

DROP INDEX IF EXISTS idx_question_bank_course;
CREATE INDEX idx_question_bank_course ON question_bank (course_id) WHERE active;
ANALYZE question_bank;
