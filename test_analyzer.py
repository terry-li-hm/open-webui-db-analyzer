#!/usr/bin/env python3
"""
Unit tests for Open WebUI Database Analyzer

Run with: python -m pytest test_analyzer.py -v
Or: python test_analyzer.py
"""

import unittest
import sqlite3
import tempfile
import os
import json
import time

from analyzer import OpenWebUIAnalyzer


class TestTimestampParsing(unittest.TestCase):
    """Test timestamp parsing with various formats."""

    def setUp(self):
        # Create minimal test database
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE user (id TEXT PRIMARY KEY, name TEXT, email TEXT, role TEXT, last_active_at INTEGER, created_at INTEGER)")
        c.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, chat TEXT, created_at INTEGER, updated_at INTEGER, archived INTEGER, pinned INTEGER, meta TEXT)")
        c.execute("CREATE TABLE feedback (id TEXT PRIMARY KEY, user_id TEXT, data TEXT, meta TEXT, created_at INTEGER)")
        conn.commit()
        conn.close()
        self.analyzer = OpenWebUIAnalyzer(self.db_path)

    def tearDown(self):
        self.analyzer.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_seconds_timestamp(self):
        """Test parsing Unix timestamp in seconds."""
        ts = 1704067200  # 2024-01-01 00:00:00 UTC
        dt = self.analyzer._parse_timestamp(ts)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2024)
        self.assertEqual(dt.month, 1)
        self.assertEqual(dt.day, 1)

    def test_milliseconds_timestamp(self):
        """Test parsing Unix timestamp in milliseconds."""
        ts = 1704067200000  # Same time in milliseconds
        dt = self.analyzer._parse_timestamp(ts)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2024)

    def test_nanoseconds_timestamp(self):
        """Test parsing Unix timestamp in nanoseconds."""
        ts = 1704067200000000000  # Same time in nanoseconds
        dt = self.analyzer._parse_timestamp(ts)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2024)

    def test_none_timestamp(self):
        """Test parsing None timestamp."""
        dt = self.analyzer._parse_timestamp(None)
        self.assertIsNone(dt)

    def test_zero_timestamp(self):
        """Test parsing zero timestamp."""
        dt = self.analyzer._parse_timestamp(0)
        self.assertIsNone(dt)


class TestRatingClassification(unittest.TestCase):
    """Test rating value classification."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE user (id TEXT PRIMARY KEY, name TEXT, email TEXT, role TEXT, last_active_at INTEGER, created_at INTEGER)")
        c.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, chat TEXT, created_at INTEGER, updated_at INTEGER, archived INTEGER, pinned INTEGER, meta TEXT)")
        c.execute("CREATE TABLE feedback (id TEXT PRIMARY KEY, user_id TEXT, data TEXT, meta TEXT, created_at INTEGER)")
        c.execute("INSERT INTO user VALUES ('u1', 'Test', 'test@test.com', 'user', 0, 0)")
        conn.commit()
        conn.close()
        self.analyzer = OpenWebUIAnalyzer(self.db_path)

    def tearDown(self):
        self.analyzer.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _add_feedback(self, rating):
        """Helper to add feedback with given rating."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        fb_id = f"fb_{time.time()}"
        data = json.dumps({"rating": rating})
        c.execute("INSERT INTO feedback VALUES (?, 'u1', ?, '{}', ?)",
                  (fb_id, data, int(time.time())))
        conn.commit()
        conn.close()

    def test_positive_int_rating(self):
        """Test integer rating 1 is counted as positive."""
        self._add_feedback(1)
        # Refresh analyzer
        self.analyzer.close()
        self.analyzer = OpenWebUIAnalyzer(self.db_path)
        checks = self.analyzer._run_sanity_checks()
        # Find feedback check
        for name, passed, details in checks:
            if "Feedback count" in name:
                self.assertIn("1👍", details)

    def test_negative_int_rating(self):
        """Test integer rating -1 is counted as negative."""
        self._add_feedback(-1)
        self.analyzer.close()
        self.analyzer = OpenWebUIAnalyzer(self.db_path)
        checks = self.analyzer._run_sanity_checks()
        for name, passed, details in checks:
            if "Feedback count" in name:
                self.assertIn("1👎", details)

    def test_string_like_rating(self):
        """Test string 'like' is counted as positive."""
        self._add_feedback("like")
        self.analyzer.close()
        self.analyzer = OpenWebUIAnalyzer(self.db_path)
        checks = self.analyzer._run_sanity_checks()
        for name, passed, details in checks:
            if "Feedback count" in name:
                self.assertIn("1👍", details)

    def test_string_dislike_rating(self):
        """Test string 'dislike' is counted as negative."""
        self._add_feedback("dislike")
        self.analyzer.close()
        self.analyzer = OpenWebUIAnalyzer(self.db_path)
        checks = self.analyzer._run_sanity_checks()
        for name, passed, details in checks:
            if "Feedback count" in name:
                self.assertIn("1👎", details)


class TestSanityChecks(unittest.TestCase):
    """Test sanity check functionality."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE user (id TEXT PRIMARY KEY, name TEXT, email TEXT, role TEXT, last_active_at INTEGER, created_at INTEGER)")
        c.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, chat TEXT, created_at INTEGER, updated_at INTEGER, archived INTEGER, pinned INTEGER, meta TEXT)")
        c.execute("CREATE TABLE feedback (id TEXT PRIMARY KEY, user_id TEXT, data TEXT, meta TEXT, created_at INTEGER)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_empty_database_passes_checks(self):
        """Empty database should pass all sanity checks."""
        analyzer = OpenWebUIAnalyzer(self.db_path)
        checks = analyzer._run_sanity_checks()
        analyzer.close()

        for name, passed, details in checks:
            self.assertTrue(passed, f"Check '{name}' failed: {details}")

    def test_orphan_chat_detected(self):
        """Chats referencing non-existent users should be detected."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        # Add chat without corresponding user
        c.execute("INSERT INTO chat VALUES ('c1', 'nonexistent_user', 'Test', '{}', 0, 0, 0, 0, '{}')")
        conn.commit()
        conn.close()

        analyzer = OpenWebUIAnalyzer(self.db_path)
        checks = analyzer._run_sanity_checks()
        analyzer.close()

        # Find the user reference check
        for name, passed, details in checks:
            if "Chat user references" in name:
                self.assertFalse(passed)
                self.assertIn("1 chats reference non-existent users", details)

    def test_valid_references_pass(self):
        """Valid user references should pass checks."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO user VALUES ('u1', 'Test', 'test@test.com', 'user', 0, 0)")
        c.execute("INSERT INTO chat VALUES ('c1', 'u1', 'Test', '{}', 0, 0, 0, 0, '{}')")
        conn.commit()
        conn.close()

        analyzer = OpenWebUIAnalyzer(self.db_path)
        checks = analyzer._run_sanity_checks()
        analyzer.close()

        for name, passed, details in checks:
            if "Chat user references" in name:
                self.assertTrue(passed)


class TestSchemaDetection(unittest.TestCase):
    """Test schema version detection."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE user (id TEXT PRIMARY KEY)")
        c.execute("CREATE TABLE chat (id TEXT PRIMARY KEY)")
        c.execute("CREATE TABLE feedback (id TEXT PRIMARY KEY)")
        c.execute("CREATE TABLE auth (id TEXT PRIMARY KEY)")
        c.execute("CREATE TABLE config (id TEXT PRIMARY KEY)")
        c.execute("CREATE TABLE alembic_version (version_num TEXT)")
        c.execute("INSERT INTO alembic_version VALUES ('abc123')")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_detects_all_expected_tables(self):
        """Should detect all expected tables."""
        analyzer = OpenWebUIAnalyzer(self.db_path)
        info = analyzer._get_schema_version()
        analyzer.close()

        self.assertEqual(info['missing_tables'], [])

    def test_detects_alembic_version(self):
        """Should detect alembic version."""
        analyzer = OpenWebUIAnalyzer(self.db_path)
        info = analyzer._get_schema_version()
        analyzer.close()

        self.assertEqual(info['alembic_version'], 'abc123')

    def test_detects_missing_tables(self):
        """Should detect missing tables."""
        # Create DB without feedback table
        db_fd2, db_path2 = tempfile.mkstemp(suffix='.db')
        conn = sqlite3.connect(db_path2)
        c = conn.cursor()
        c.execute("CREATE TABLE user (id TEXT PRIMARY KEY)")
        c.execute("CREATE TABLE chat (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()

        analyzer = OpenWebUIAnalyzer(db_path2)
        info = analyzer._get_schema_version()
        analyzer.close()

        os.close(db_fd2)
        os.unlink(db_path2)

        self.assertIn('feedback', info['missing_tables'])
        self.assertIn('auth', info['missing_tables'])
        self.assertIn('config', info['missing_tables'])


class TestParseTracking(unittest.TestCase):
    """Test parse success/failure tracking."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE user (id TEXT PRIMARY KEY, name TEXT, email TEXT, role TEXT, last_active_at INTEGER, created_at INTEGER)")
        c.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, chat TEXT, created_at INTEGER, updated_at INTEGER, archived INTEGER, pinned INTEGER, meta TEXT)")
        c.execute("CREATE TABLE feedback (id TEXT PRIMARY KEY, user_id TEXT, data TEXT, meta TEXT, created_at INTEGER)")
        c.execute("INSERT INTO user VALUES ('u1', 'Test', 'test@test.com', 'user', 0, 0)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_tracks_successful_parse(self):
        """Should track successful parses."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO chat VALUES ('c1', 'u1', 'Test', ?, 0, 0, 0, 0, '{}')",
                  (json.dumps({"messages": []}),))
        conn.commit()
        conn.close()

        analyzer = OpenWebUIAnalyzer(self.db_path)
        analyzer.chat_volume()
        analyzer.close()

        self.assertEqual(analyzer._parse_stats['chat messages'][0], 1)  # success
        self.assertEqual(analyzer._parse_stats['chat messages'][1], 1)  # total

    def test_tracks_failed_parse(self):
        """Should track failed parses."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO chat VALUES ('c1', 'u1', 'Test', 'invalid json', 0, 0, 0, 0, '{}')")
        conn.commit()
        conn.close()

        analyzer = OpenWebUIAnalyzer(self.db_path)
        analyzer.chat_volume()
        analyzer.close()

        self.assertEqual(analyzer._parse_stats['chat messages'][0], 0)  # success
        self.assertEqual(analyzer._parse_stats['chat messages'][1], 1)  # total
        self.assertEqual(analyzer._parse_errors['chat_volume/messages'], 1)


if __name__ == '__main__':
    unittest.main()
