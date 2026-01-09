#!/usr/bin/env python3
"""
Open WebUI Database Analyzer
Analyzes webui.db SQLite database from Open WebUI (tested with v0.6.30)

Usage:
    python analyzer.py <path_to_webui.db> [command]

Commands:
    summary     - Overview of all tables and record counts (default)
    chats       - Chat volume analysis
    users       - User statistics
    timeline    - Chat activity over time
    models      - Model usage statistics
    export      - Export chat data to JSON
"""

import sqlite3
import json
import sys
import os
from datetime import datetime
from collections import defaultdict
from pathlib import Path


class OpenWebUIAnalyzer:
    """Analyzer for Open WebUI SQLite database."""

    def __init__(self, db_path: str):
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database not found: {db_path}")
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()

    def get_tables(self) -> list[dict]:
        """Get all tables and their row counts."""
        self.cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """)
        tables = []
        for row in self.cursor.fetchall():
            table_name = row['name']
            self.cursor.execute(f"SELECT COUNT(*) as count FROM [{table_name}]")
            count = self.cursor.fetchone()['count']
            tables.append({'name': table_name, 'count': count})
        return tables

    def get_table_schema(self, table_name: str) -> list[dict]:
        """Get schema for a specific table."""
        self.cursor.execute(f"PRAGMA table_info([{table_name}])")
        return [dict(row) for row in self.cursor.fetchall()]

    def summary(self):
        """Print database summary."""
        print("=" * 60)
        print("OPEN WEBUI DATABASE SUMMARY")
        print("=" * 60)
        print(f"Database: {self.db_path}")
        print(f"Size: {os.path.getsize(self.db_path) / (1024*1024):.2f} MB")
        print()

        tables = self.get_tables()
        print(f"{'Table':<25} {'Records':>10}")
        print("-" * 37)
        total = 0
        for t in tables:
            print(f"{t['name']:<25} {t['count']:>10,}")
            total += t['count']
        print("-" * 37)
        print(f"{'TOTAL':<25} {total:>10,}")
        print()

    def chat_volume(self):
        """Analyze chat volume statistics."""
        print("=" * 60)
        print("CHAT VOLUME ANALYSIS")
        print("=" * 60)

        # Total chats
        self.cursor.execute("SELECT COUNT(*) as count FROM chat")
        total_chats = self.cursor.fetchone()['count']
        print(f"\nTotal Chats: {total_chats:,}")

        # Archived vs active
        self.cursor.execute("""
            SELECT
                SUM(CASE WHEN archived = 1 THEN 1 ELSE 0 END) as archived,
                SUM(CASE WHEN archived = 0 OR archived IS NULL THEN 1 ELSE 0 END) as active,
                SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) as pinned
            FROM chat
        """)
        row = self.cursor.fetchone()
        print(f"  - Active: {row['active']:,}")
        print(f"  - Archived: {row['archived']:,}")
        print(f"  - Pinned: {row['pinned']:,}")

        # Chats per user
        print("\n" + "-" * 40)
        print("CHATS PER USER")
        print("-" * 40)
        self.cursor.execute("""
            SELECT u.name, u.email, COUNT(c.id) as chat_count
            FROM chat c
            LEFT JOIN user u ON c.user_id = u.id
            GROUP BY c.user_id
            ORDER BY chat_count DESC
            LIMIT 20
        """)
        rows = self.cursor.fetchall()
        print(f"{'User':<30} {'Email':<30} {'Chats':>8}")
        print("-" * 70)
        for row in rows:
            name = row['name'] or 'Unknown'
            email = row['email'] or 'N/A'
            print(f"{name[:29]:<30} {email[:29]:<30} {row['chat_count']:>8,}")

        # Message counts (from chat JSON)
        print("\n" + "-" * 40)
        print("MESSAGE STATISTICS")
        print("-" * 40)
        self.cursor.execute("SELECT id, chat FROM chat")
        total_messages = 0
        user_messages = 0
        assistant_messages = 0

        for row in self.cursor.fetchall():
            try:
                chat_data = json.loads(row['chat']) if row['chat'] else {}
                messages = chat_data.get('messages', [])
                # Handle both old format (list) and new format (dict with history)
                if isinstance(messages, dict):
                    messages = messages.get('messages', [])
                for msg in messages:
                    total_messages += 1
                    role = msg.get('role', '')
                    if role == 'user':
                        user_messages += 1
                    elif role == 'assistant':
                        assistant_messages += 1
            except (json.JSONDecodeError, TypeError):
                pass

        print(f"Total Messages: {total_messages:,}")
        print(f"  - User messages: {user_messages:,}")
        print(f"  - Assistant messages: {assistant_messages:,}")
        if total_chats > 0:
            print(f"  - Avg messages per chat: {total_messages / total_chats:.1f}")
        print()

    def user_stats(self):
        """Analyze user statistics."""
        print("=" * 60)
        print("USER STATISTICS")
        print("=" * 60)

        # Total users
        self.cursor.execute("SELECT COUNT(*) as count FROM user")
        total_users = self.cursor.fetchone()['count']
        print(f"\nTotal Users: {total_users:,}")

        # Users by role
        self.cursor.execute("""
            SELECT role, COUNT(*) as count
            FROM user
            GROUP BY role
            ORDER BY count DESC
        """)
        print("\nUsers by Role:")
        for row in self.cursor.fetchall():
            print(f"  - {row['role']}: {row['count']:,}")

        # User activity
        print("\n" + "-" * 40)
        print("USER ACTIVITY (Last Active)")
        print("-" * 40)
        self.cursor.execute("""
            SELECT name, email, role, last_active_at, created_at,
                   (SELECT COUNT(*) FROM chat WHERE chat.user_id = user.id) as chat_count
            FROM user
            ORDER BY last_active_at DESC
            LIMIT 15
        """)
        print(f"{'Name':<20} {'Role':<10} {'Chats':>6} {'Last Active':<20}")
        print("-" * 60)
        for row in self.cursor.fetchall():
            name = (row['name'] or 'Unknown')[:19]
            last_active = self._format_timestamp(row['last_active_at'])
            print(f"{name:<20} {row['role']:<10} {row['chat_count']:>6} {last_active:<20}")
        print()

    def timeline(self):
        """Analyze chat activity over time."""
        print("=" * 60)
        print("CHAT TIMELINE ANALYSIS")
        print("=" * 60)

        # Chats by month
        self.cursor.execute("""
            SELECT created_at FROM chat ORDER BY created_at
        """)

        monthly = defaultdict(int)
        daily = defaultdict(int)
        hourly = defaultdict(int)

        for row in self.cursor.fetchall():
            ts = row['created_at']
            if ts:
                dt = self._parse_timestamp(ts)
                if dt:
                    monthly[dt.strftime('%Y-%m')] += 1
                    daily[dt.strftime('%Y-%m-%d')] += 1
                    hourly[dt.hour] += 1

        # Monthly breakdown
        print("\nCHATS BY MONTH")
        print("-" * 30)
        for month in sorted(monthly.keys()):
            bar = '█' * min(50, monthly[month] // max(1, max(monthly.values()) // 50))
            print(f"{month}: {monthly[month]:>5} {bar}")

        # Hourly distribution
        print("\nCHATS BY HOUR OF DAY")
        print("-" * 30)
        max_hourly = max(hourly.values()) if hourly else 1
        for hour in range(24):
            count = hourly.get(hour, 0)
            bar = '█' * (count * 30 // max(1, max_hourly))
            print(f"{hour:02d}:00 {count:>5} {bar}")

        # Recent activity
        print("\nRECENT DAILY ACTIVITY (Last 14 days)")
        print("-" * 30)
        recent_days = sorted(daily.keys())[-14:]
        for day in recent_days:
            bar = '█' * min(40, daily[day])
            print(f"{day}: {daily[day]:>4} {bar}")
        print()

    def model_usage(self):
        """Analyze model usage from chats."""
        print("=" * 60)
        print("MODEL USAGE ANALYSIS")
        print("=" * 60)

        self.cursor.execute("SELECT chat FROM chat")
        model_counts = defaultdict(int)

        for row in self.cursor.fetchall():
            try:
                chat_data = json.loads(row['chat']) if row['chat'] else {}
                # Try different locations where model info might be stored
                model = chat_data.get('model') or chat_data.get('models', [None])[0] if chat_data.get('models') else None

                # Also check in messages for model info
                messages = chat_data.get('messages', [])
                if isinstance(messages, dict):
                    messages = messages.get('messages', [])
                for msg in messages:
                    if msg.get('role') == 'assistant':
                        m = msg.get('model') or msg.get('modelName')
                        if m:
                            model = m
                            break

                if model:
                    model_counts[model] += 1
                else:
                    model_counts['(unknown)'] += 1
            except (json.JSONDecodeError, TypeError, IndexError):
                model_counts['(parse error)'] += 1

        print(f"\n{'Model':<50} {'Chats':>10}")
        print("-" * 62)
        for model, count in sorted(model_counts.items(), key=lambda x: -x[1]):
            print(f"{model[:49]:<50} {count:>10,}")
        print()

    def export_chats(self, output_path: str = None):
        """Export all chats to JSON."""
        if output_path is None:
            output_path = "chats_export.json"

        print(f"Exporting chats to {output_path}...")

        self.cursor.execute("""
            SELECT c.*, u.name as user_name, u.email as user_email
            FROM chat c
            LEFT JOIN user u ON c.user_id = u.id
            ORDER BY c.created_at DESC
        """)

        chats = []
        for row in self.cursor.fetchall():
            chat = dict(row)
            # Parse JSON fields
            try:
                chat['chat'] = json.loads(chat['chat']) if chat['chat'] else None
            except json.JSONDecodeError:
                pass
            try:
                chat['meta'] = json.loads(chat['meta']) if chat['meta'] else None
            except json.JSONDecodeError:
                pass
            # Convert timestamps
            chat['created_at_formatted'] = self._format_timestamp(chat['created_at'])
            chat['updated_at_formatted'] = self._format_timestamp(chat['updated_at'])
            chats.append(chat)

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(chats, f, indent=2, ensure_ascii=False, default=str)

        print(f"Exported {len(chats)} chats to {output_path}")

    def _parse_timestamp(self, ts) -> datetime | None:
        """Parse timestamp (could be seconds or nanoseconds)."""
        if not ts:
            return None
        try:
            # If timestamp is very large, it's likely nanoseconds
            if ts > 1e12:
                ts = ts / 1e9
            elif ts > 1e10:
                ts = ts / 1e3
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError, OverflowError):
            return None

    def _format_timestamp(self, ts) -> str:
        """Format timestamp for display."""
        dt = self._parse_timestamp(ts)
        return dt.strftime('%Y-%m-%d %H:%M') if dt else 'N/A'


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nError: Please provide path to webui.db")
        sys.exit(1)

    db_path = sys.argv[1]
    command = sys.argv[2] if len(sys.argv) > 2 else 'summary'

    try:
        with OpenWebUIAnalyzer(db_path) as analyzer:
            if command == 'summary':
                analyzer.summary()
                analyzer.chat_volume()
            elif command == 'chats':
                analyzer.chat_volume()
            elif command == 'users':
                analyzer.user_stats()
            elif command == 'timeline':
                analyzer.timeline()
            elif command == 'models':
                analyzer.model_usage()
            elif command == 'export':
                output = sys.argv[3] if len(sys.argv) > 3 else None
                analyzer.export_chats(output)
            elif command == 'all':
                analyzer.summary()
                analyzer.chat_volume()
                analyzer.user_stats()
                analyzer.timeline()
                analyzer.model_usage()
            else:
                print(f"Unknown command: {command}")
                print(__doc__)
                sys.exit(1)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
