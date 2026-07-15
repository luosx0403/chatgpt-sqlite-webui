BEGIN TRANSACTION;
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
INSERT INTO "conversation_nodes" VALUES('legacy-synthetic','root',NULL,'["u"]',NULL,NULL,NULL,NULL,NULL,NULL,'','98350d7ed32158f12dabb5e48c4418a6000333a006277b81cfd75a19130915cf','{"content_notes":[],"message_metadata":null,"node_metadata":null}',1,NULL,1);
INSERT INTO "conversation_nodes" VALUES('legacy-synthetic','u','root','["a"]','mu','user',NULL,1700000000.0,NULL,'text','synthetic legacy user','2f7cbfc0dfb3d33207d8c342275835f4213a48a80c0324ec0f7883197e243b3f','{"content_notes":[],"message_metadata":{},"node_metadata":null}',1,'{"author":{"name":null,"role":"user"},"content":{"content_type":"text","parts":["synthetic legacy user"]},"create_time":1700000000,"id":"mu","metadata":{},"update_time":null}',1);
INSERT INTO "conversation_nodes" VALUES('legacy-synthetic','a','u','[]','ma','assistant',NULL,1700000001.0,NULL,'text','synthetic legacy assistant','591e85073ac16ab45b7c530f19c48b21452a1150cc7c24eba3947bbca4aeb167','{"content_notes":[],"message_metadata":{},"node_metadata":null}',1,'{"author":{"name":null,"role":"assistant"},"content":{"content_type":"text","parts":["synthetic legacy assistant"]},"create_time":1700000001,"id":"ma","metadata":{},"update_time":null}',1);
CREATE TABLE conversations (
            conversation_id TEXT PRIMARY KEY,
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
INSERT INTO "conversations" VALUES('legacy-synthetic',NULL,'Legacy Synthetic',1700000000.0,1700000001.0,'a','conversations.json',0,'71fc0d6155bd0ff7a42f9435f29a3d398d13bedae7844ebe815e29557a4b271f',1,NULL,NULL,NULL,'{}');
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
INSERT INTO "file_index" VALUES(1,1,'conversations.json','json','.json',743,NULL,NULL,NULL);
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
INSERT INTO "import_runs" VALUES(1,'/private/tmp/chatgpt-legacy-fa37.0Cs6Zw/synthetic.zip','zip',NULL,877,'2026-07-15T16:43:21+00:00','2026-07-15T16:43:21+00:00','finished','{"close_seconds":0.00248,"finalize_commit_seconds":0.000118,"fts_rebuild_seconds":0.0,"import_index_rebuild_seconds":4.1e-05,"import_run_id":1,"inserted_conversations":1,"legacy_pre_commit_seconds":0.007391,"nodes":3,"parse_and_upsert_seconds":0.000346,"pragma_optimize_seconds":0.0,"skipped_invalid_elements":0,"source":"zip","source_scan_seconds":0.000817,"total_import_seconds":0.010027,"unchanged_conversations":0,"updated_conversations":0,"valid_conversations":1,"wall_total_seconds":0.010027,"warnings":0}');
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
CREATE VIRTUAL TABLE message_fts
            USING fts5(conversation_id UNINDEXED, node_id UNINDEXED, role UNINDEXED, content_text);
INSERT INTO "message_fts" VALUES('legacy-synthetic','u','user','synthetic legacy user');
INSERT INTO "message_fts" VALUES('legacy-synthetic','a','assistant','synthetic legacy assistant');
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
INSERT INTO "source_files" VALUES(1,1,'conversations.json','json',743,NULL,1,1);
CREATE INDEX idx_conversations_times
            ON conversations(create_time, update_time);
CREATE INDEX idx_warnings_run
            ON import_warnings(import_run_id, warning_type);
CREATE INDEX idx_nodes_conversation_path
            ON conversation_nodes(conversation_id, is_on_current_path)
        ;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('import_runs',1);
INSERT INTO "sqlite_sequence" VALUES('source_files',1);
INSERT INTO "sqlite_sequence" VALUES('file_index',1);
COMMIT;
