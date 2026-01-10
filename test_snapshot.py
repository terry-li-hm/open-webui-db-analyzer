#!/usr/bin/env python3
"""
Snapshot tests for Open WebUI Database Analyzer

Creates a database with known data and compares analyzer output against
saved snapshots. This ensures output format remains consistent and
calculations are correct.

Run with: python test_snapshot.py [--update]
  --update: Update snapshots with current output
"""

import subprocess
import sys
import os
import sqlite3
import json
import tempfile
import shutil
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SNAPSHOT_DIR = SCRIPT_DIR / "snapshots"
ANALYZER_PATH = SCRIPT_DIR / "analyzer.py"


def create_known_database(db_path: str):
    """Create a database with known, deterministic data."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Create tables
    c.execute("""
        CREATE TABLE user (
            id TEXT PRIMARY KEY,
            name TEXT,
            email TEXT,
            role TEXT DEFAULT 'user',
            last_active_at INTEGER,
            created_at INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE chat (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            title TEXT,
            chat TEXT,
            created_at INTEGER,
            updated_at INTEGER,
            archived INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0,
            meta TEXT
        )
    """)

    c.execute("""
        CREATE TABLE feedback (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            data TEXT,
            meta TEXT,
            created_at INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE auth (id TEXT PRIMARY KEY)
    """)

    c.execute("""
        CREATE TABLE config (id TEXT PRIMARY KEY)
    """)

    c.execute("""
        CREATE TABLE alembic_version (version_num TEXT)
    """)
    c.execute("INSERT INTO alembic_version VALUES ('snapshot_test_v1')")

    # Fixed timestamp: 2024-06-15 12:00:00 UTC
    base_ts = 1718452800

    # Insert users
    users = [
        ("user_alice", "Alice Smith", "alice@example.com", "admin", base_ts, base_ts - 86400*30),
        ("user_bob", "Bob Jones", "bob@example.com", "user", base_ts - 3600, base_ts - 86400*60),
        ("user_carol", "Carol White", "carol@example.com", "user", base_ts - 7200, base_ts - 86400*90),
    ]
    c.executemany("INSERT INTO user VALUES (?, ?, ?, ?, ?, ?)", users)

    # Insert chats with known message structures
    chats = [
        # Alice's chats
        ("chat_1", "user_alice", "Chat about Python",
         json.dumps({"messages": [
             {"role": "user", "content": "How do I use Python?"},
             {"role": "assistant", "content": "Python is easy!", "model": "gpt-4"}
         ]}), base_ts - 1000),
        ("chat_2", "user_alice", "Chat about JavaScript",
         json.dumps({"messages": [
             {"role": "user", "content": "What is JavaScript?"},
             {"role": "assistant", "content": "JS is for web!", "model": "gpt-4"}
         ]}), base_ts - 2000),
        ("chat_3", "user_alice", "Chat about Rust",
         json.dumps({"messages": [
             {"role": "user", "content": "Is Rust fast?"},
             {"role": "assistant", "content": "Very fast!", "model": "claude-3"}
         ]}), base_ts - 3000),

        # Bob's chats
        ("chat_4", "user_bob", "Help with coding",
         json.dumps({"messages": [
             {"role": "user", "content": "Help me code"},
             {"role": "assistant", "content": "Sure!", "model": "gpt-4"}
         ]}), base_ts - 4000),
        ("chat_5", "user_bob", "Debug my app",
         json.dumps({"messages": [
             {"role": "user", "content": "Why error?"},
             {"role": "assistant", "content": "Check logs", "model": "claude-3"}
         ]}), base_ts - 5000),

        # Carol's chats
        ("chat_6", "user_carol", "Learn AI",
         json.dumps({"messages": [
             {"role": "user", "content": "What is AI?"},
             {"role": "assistant", "content": "Artificial Intelligence", "model": "gpt-4"}
         ]}), base_ts - 6000),
    ]
    for chat in chats:
        c.execute("INSERT INTO chat (id, user_id, title, chat, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (chat[0], chat[1], chat[2], chat[3], chat[4], chat[4]))

    # Insert feedback
    feedbacks = [
        # Positive feedback
        ("fb_1", "user_alice", json.dumps({"rating": 1, "model_id": "gpt-4"}),
         json.dumps({"chat_id": "chat_1", "message_id": "msg-1"}), base_ts - 100),
        ("fb_2", "user_alice", json.dumps({"rating": 1, "model_id": "gpt-4"}),
         json.dumps({"chat_id": "chat_2", "message_id": "msg-1"}), base_ts - 200),
        ("fb_3", "user_bob", json.dumps({"rating": 1, "model_id": "gpt-4"}),
         json.dumps({"chat_id": "chat_4", "message_id": "msg-1"}), base_ts - 300),

        # Negative feedback
        ("fb_4", "user_alice", json.dumps({"rating": -1, "model_id": "claude-3"}),
         json.dumps({"chat_id": "chat_3", "message_id": "msg-1"}), base_ts - 400),
        ("fb_5", "user_bob", json.dumps({"rating": -1, "model_id": "claude-3"}),
         json.dumps({"chat_id": "chat_5", "message_id": "msg-1"}), base_ts - 500),
    ]
    c.executemany("INSERT INTO feedback VALUES (?, ?, ?, ?, ?)", feedbacks)

    conn.commit()
    conn.close()


def run_analyzer(db_path: str, command: str) -> str:
    """Run analyzer and return output."""
    result = subprocess.run(
        [sys.executable, str(ANALYZER_PATH), db_path, command, "--all-users"],
        capture_output=True,
        text=True
    )
    return result.stdout


def normalize_output(output: str) -> str:
    """Normalize output for comparison (remove variable parts)."""
    lines = []
    for line in output.split('\n'):
        # Skip lines with file paths (they contain temp paths)
        if 'Database:' in line or '/tmp/' in line or '/var/' in line:
            continue
        # Skip size line (may vary)
        if 'Size:' in line and 'MB' in line:
            continue
        lines.append(line)
    return '\n'.join(lines)


def load_snapshot(name: str) -> str | None:
    """Load a saved snapshot."""
    path = SNAPSHOT_DIR / f"{name}.txt"
    if path.exists():
        return path.read_text()
    return None


def save_snapshot(name: str, content: str):
    """Save a snapshot."""
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.txt"
    path.write_text(content)
    print(f"  Saved snapshot: {path}")


def compare_snapshots(name: str, actual: str, expected: str) -> bool:
    """Compare actual output with expected snapshot."""
    actual_lines = actual.strip().split('\n')
    expected_lines = expected.strip().split('\n')

    if actual_lines == expected_lines:
        return True

    # Show diff
    print(f"\n  Snapshot mismatch for '{name}':")
    print("  " + "-" * 50)

    # Simple diff - show first difference
    for i, (a, e) in enumerate(zip(actual_lines, expected_lines)):
        if a != e:
            print(f"  Line {i+1} differs:")
            print(f"    Expected: {e[:80]}")
            print(f"    Actual:   {a[:80]}")
            break

    if len(actual_lines) != len(expected_lines):
        print(f"  Line count differs: expected {len(expected_lines)}, got {len(actual_lines)}")

    return False


def main():
    update_mode = "--update" in sys.argv

    # Create temp database
    temp_dir = tempfile.mkdtemp(prefix="snapshot_test_")
    db_path = os.path.join(temp_dir, "test.db")

    try:
        print("Creating test database...")
        create_known_database(db_path)

        # Test cases: (name, command)
        test_cases = [
            ("summary", "summary"),
            ("feedback", "feedback"),
            ("models", "models"),
            ("users", "users"),
        ]

        all_passed = True

        for name, command in test_cases:
            print(f"\nTesting '{name}' command...")
            actual = normalize_output(run_analyzer(db_path, command))

            if update_mode:
                save_snapshot(name, actual)
            else:
                expected = load_snapshot(name)
                if expected is None:
                    print(f"  ⚠️  No snapshot found for '{name}'. Run with --update to create.")
                    all_passed = False
                else:
                    if compare_snapshots(name, actual, expected):
                        print(f"  ✓ Snapshot matches")
                    else:
                        all_passed = False

        print("\n" + "=" * 60)
        if update_mode:
            print("Snapshots updated successfully!")
        elif all_passed:
            print("✓ All snapshot tests passed!")
        else:
            print("✗ Some snapshot tests failed")
            return 1

        return 0

    finally:
        shutil.rmtree(temp_dir)


if __name__ == "__main__":
    sys.exit(main())
