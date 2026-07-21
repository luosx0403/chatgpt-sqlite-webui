-- Frozen from production builder commit 81a6de4fe61b6fba4a4e25006231426211b17b06 with the supported no-trigram runtime path.
BEGIN TRANSACTION;
CREATE TABLE archive_generations (
    name TEXT NOT NULL PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0
);
INSERT INTO "archive_generations" VALUES('title',1);
INSERT INTO "archive_generations" VALUES('message',1);
CREATE TABLE conversation_nodes (
        conversation_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        parent_node_id TEXT,
        children_json TEXT,
        message_id TEXT,
        role TEXT,
        author_name TEXT,
        create_time REAL,
        update_time REAL,
        content_type TEXT,
        content_text TEXT,
        content_hash TEXT,
        metadata_json TEXT,
        is_on_current_path INTEGER NOT NULL DEFAULT 0,
        raw_message_json TEXT,
        last_import_run_id INTEGER,
        PRIMARY KEY(conversation_id, node_id),
        FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
        FOREIGN KEY(last_import_run_id) REFERENCES import_runs(id)
    );
INSERT INTO "conversation_nodes" VALUES('format3-conversation','format3-node',NULL,NULL,NULL,'assistant',NULL,NULL,NULL,'text','frozen format three body','format3-content-hash',NULL,1,NULL,NULL);
CREATE TABLE conversations (
        conversation_id TEXT NOT NULL PRIMARY KEY,
        exported_id TEXT,
        title TEXT,
        create_time REAL,
        update_time REAL,
        current_node TEXT,
        source_file TEXT,
        source_array_index INTEGER,
        aggregate_hash TEXT NOT NULL,
        last_import_run_id INTEGER,
        is_archived INTEGER,
        is_starred INTEGER,
        default_model_slug TEXT,
        metadata_json TEXT,
        FOREIGN KEY(last_import_run_id) REFERENCES import_runs(id)
    );
INSERT INTO "conversations" VALUES('format3-conversation',NULL,'Frozen format 3 title',NULL,NULL,'format3-node',NULL,NULL,'format3-hash',NULL,NULL,NULL,NULL,NULL);
CREATE TABLE exports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        format TEXT NOT NULL,
        output_path TEXT NOT NULL,
        output_hash TEXT NOT NULL,
        exported_at TEXT NOT NULL,
        export_options_json TEXT,
        UNIQUE(conversation_id, format, output_path)
    );
CREATE TABLE file_index (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        import_run_id INTEGER NOT NULL,
        source_path TEXT NOT NULL,
        file_type TEXT NOT NULL,
        extension TEXT,
        size INTEGER,
        sha256 TEXT,
        related_conversation_id TEXT,
        related_message_id TEXT,
        FOREIGN KEY(import_run_id) REFERENCES import_runs(id)
    );
CREATE TABLE import_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        input_path TEXT NOT NULL,
        input_kind TEXT NOT NULL,
        input_sha256 TEXT,
        input_size INTEGER,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        summary_json TEXT
    );
CREATE TABLE import_warnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        import_run_id INTEGER NOT NULL,
        source_file TEXT NOT NULL,
        array_index INTEGER,
        warning_type TEXT NOT NULL,
        keys_json TEXT,
        raw_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(import_run_id) REFERENCES import_runs(id)
    );
CREATE TABLE source_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        import_run_id INTEGER NOT NULL,
        source_path TEXT NOT NULL,
        file_type TEXT NOT NULL,
        size INTEGER,
        sha256 TEXT,
        is_conversation_json INTEGER NOT NULL DEFAULT 0,
        is_selected_conversation_source INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(import_run_id) REFERENCES import_runs(id)
    );
CREATE TABLE web_index_metadata(
                   key TEXT NOT NULL PRIMARY KEY,
                   value TEXT NOT NULL
               );
INSERT INTO "web_index_metadata" VALUES('message_norm_text','normalized');
INSERT INTO "web_index_metadata" VALUES('title_norm_text','normalized');
INSERT INTO "web_index_metadata" VALUES('web_index_format_version','3');
INSERT INTO "web_index_metadata" VALUES('display_text_resolver_version','1');
INSERT INTO "web_index_metadata" VALUES('normalization_index_format_version','1');
INSERT INTO "web_index_metadata" VALUES('oversized_fallback','required');
INSERT INTO "web_index_metadata" VALUES('max_input_bytes','4194304');
INSERT INTO "web_index_metadata" VALUES('max_normalized_bytes','2097152');
INSERT INTO "web_index_metadata" VALUES('max_derived_bytes','8388608');
INSERT INTO "web_index_metadata" VALUES('message_generation','1');
INSERT INTO "web_index_metadata" VALUES('title_generation','1');
CREATE TABLE web_index_oversized(
                   kind TEXT NOT NULL,
                   source_rowid INTEGER NOT NULL,
                   conversation_id TEXT NOT NULL,
                   node_id TEXT,
                   input_bytes INTEGER NOT NULL,
                   reason TEXT NOT NULL,
                   PRIMARY KEY(kind, source_rowid)
               );
CREATE TABLE web_message_norm(
                   conversation_id TEXT NOT NULL,
                   node_id TEXT NOT NULL,
                   content_norm TEXT NOT NULL,
                   PRIMARY KEY(conversation_id, node_id)
               );
INSERT INTO "web_message_norm" VALUES('format3-conversation','format3-node','frozen format three body');
CREATE TABLE web_title_norm(
                   conversation_id TEXT NOT NULL PRIMARY KEY,
                   title_norm TEXT NOT NULL
               );
INSERT INTO "web_title_norm" VALUES('format3-conversation','frozen format 3 title');
CREATE TRIGGER archive_title_generation_insert
        AFTER INSERT ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'title';
        END;
CREATE TRIGGER archive_title_generation_update
        AFTER UPDATE OF conversation_id, title ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'title';
        END;
CREATE TRIGGER archive_title_generation_delete
        AFTER DELETE ON conversations BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'title';
        END;
CREATE TRIGGER archive_message_generation_insert
        AFTER INSERT ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'message';
        END;
CREATE TRIGGER archive_message_generation_update
        AFTER UPDATE OF conversation_id, node_id, content_text, raw_message_json ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'message';
        END;
CREATE TRIGGER archive_message_generation_delete
        AFTER DELETE ON conversation_nodes BEGIN
            UPDATE archive_generations SET generation = generation + 1 WHERE name = 'message';
        END;
CREATE INDEX idx_nodes_conversation_path
        ON conversation_nodes(conversation_id, is_on_current_path);
CREATE INDEX idx_nodes_conversation_flag_parent
        ON conversation_nodes(conversation_id, is_on_current_path, parent_node_id);
CREATE INDEX idx_conversations_times
        ON conversations(create_time, update_time);
CREATE INDEX idx_warnings_run
        ON import_warnings(import_run_id, warning_type);
DELETE FROM "sqlite_sequence";
COMMIT;
