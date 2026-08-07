-- 0024_feedback_report_images.sql
-- Screenshots attached to a feedback report (0023_feedback_reports.sql).
-- Own table rather than a column on feedback_reports because a report can
-- carry zero-to-several images (see utils/uploads.py's
-- MAX_FEEDBACK_IMAGES) -- same one-to-many reasoning as any other
-- attachment list, not squeezed into a delimited string column.
CREATE TABLE feedback_report_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_report_id INTEGER NOT NULL,
    image_path TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (feedback_report_id) REFERENCES feedback_reports(id)
);

CREATE INDEX idx_feedback_report_images_report_id ON feedback_report_images(feedback_report_id);
