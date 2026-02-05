#!/usr/bin/env python3
"""
Open WebUI Database Analyzer
Analyzes webui.db SQLite database from Open WebUI (tested with v0.6.30)

Usage:
    python analyzer.py <path_to_webui.db> [command] [options]

Commands:
    summary     - Overview of all tables and record counts (default)
    chats       - Chat volume analysis
    users       - User statistics
    timeline    - Chat activity over time
    usage       - Per-user per-month chat counts
    models      - Model usage statistics
    feedback    - Thumbs up/down feedback statistics
    plot        - Generate trend chart (usage + accuracy over time)
    changes     - Recent config changes (models, knowledge, functions, tools, files)
    verify      - Verify data accuracy with cross-checks and samples
    compare     - Compare DB against Open WebUI JSON export (requires --export-file)
    export      - Export chat data to JSON

Options:
    --all           Show all users (default: hide users with <500 chats)
    --min-chats N   Minimum chats to show user (default: 500)
    --days N        Days to look back for changes command (default: 7)
    --export-file   Path to Open WebUI feedback JSON export (for compare command)
    --debug         Show debug info for parse errors and unknown rating values
"""

from __future__ import annotations

import sqlite3
import json
import sys
import os
import argparse
from datetime import datetime
from collections import defaultdict
from pathlib import Path

# Default minimum chats to display a user
DEFAULT_MIN_CHATS = 500


class OpenWebUIAnalyzer:
    """Analyzer for Open WebUI SQLite database."""

    def __init__(self, db_path: str, debug: bool = False):
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database not found: {db_path}")
        self.db_path = db_path
        self.debug = debug
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        # Error tracking
        self._parse_errors = defaultdict(int)
        self._unknown_ratings = defaultdict(int)
        # Parse success tracking: {context: (success_count, total_count)}
        self._parse_stats = defaultdict(lambda: [0, 0])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()

    def _track_error(self, context: str, error: Exception = None):
        """Track a parse error for later reporting."""
        self._parse_errors[context] += 1
        if self.debug and error:
            print(f"  [DEBUG] Parse error in {context}: {error}")

    def _track_unknown_rating(self, rating_value):
        """Track an unrecognized rating value."""
        key = f"{type(rating_value).__name__}:{repr(rating_value)}"
        self._unknown_ratings[key] += 1
        if self.debug:
            print(f"  [DEBUG] Unknown rating: {key}")

    def _track_parse(self, context: str, success: bool):
        """Track parse attempt for success rate calculation."""
        self._parse_stats[context][1] += 1  # total
        if success:
            self._parse_stats[context][0] += 1  # success

    def _run_sanity_checks(self) -> list[tuple[str, bool, str]]:
        """Run sanity checks on data consistency. Returns list of (check_name, passed, details)."""
        checks = []

        # Check 1: Sum of per-user chats equals total chats
        self.cursor.execute("SELECT COUNT(*) as count FROM chat")
        total_chats = self.cursor.fetchone()['count']

        self.cursor.execute("SELECT SUM(cnt) as total FROM (SELECT COUNT(*) as cnt FROM chat GROUP BY user_id)")
        row = self.cursor.fetchone()
        sum_user_chats = row['total'] if row['total'] else 0

        passed = total_chats == sum_user_chats
        checks.append((
            "Chat count consistency",
            passed,
            f"Total: {total_chats}, Sum by user: {sum_user_chats}" if not passed else "OK"
        ))

        # Check 2: All chat.user_id references exist in user table
        self.cursor.execute("""
            SELECT COUNT(*) as count FROM chat
            WHERE user_id NOT IN (SELECT id FROM user)
        """)
        orphan_chats = self.cursor.fetchone()['count']
        passed = orphan_chats == 0
        checks.append((
            "Chat user references valid",
            passed,
            f"{orphan_chats} chats reference non-existent users" if not passed else "OK"
        ))

        # Check 3: All feedback.user_id references exist in user table
        self.cursor.execute("""
            SELECT name FROM sqlite_master WHERE type='table' AND name='feedback'
        """)
        if self.cursor.fetchone():
            self.cursor.execute("""
                SELECT COUNT(*) as count FROM feedback
                WHERE user_id NOT IN (SELECT id FROM user)
            """)
            orphan_feedback = self.cursor.fetchone()['count']
            passed = orphan_feedback == 0
            checks.append((
                "Feedback user references valid",
                passed,
                f"{orphan_feedback} feedbacks reference non-existent users" if not passed else "OK"
            ))

        # Check 4: Feedback thumbs up + down + neutral = total feedback
        self.cursor.execute("SELECT COUNT(*) as count FROM feedback")
        total_feedback = self.cursor.fetchone()['count']

        if total_feedback > 0:
            self.cursor.execute("SELECT data FROM feedback")
            up = down = neutral = parse_fail = 0
            for row in self.cursor.fetchall():
                try:
                    data = json.loads(row['data']) if row['data'] else {}
                    rating = data.get('rating')
                    if rating is not None:
                        if isinstance(rating, (int, float)):
                            if rating > 0:
                                up += 1
                            elif rating < 0:
                                down += 1
                            else:
                                neutral += 1
                        elif isinstance(rating, str) and rating.lower() in ('1', 'like', 'positive', 'up', 'good', 'yes'):
                            up += 1
                        elif isinstance(rating, str) and rating.lower() in ('-1', 'dislike', 'negative', 'down', 'bad', 'no', '0'):
                            down += 1
                        else:
                            neutral += 1
                    else:
                        neutral += 1
                except (json.JSONDecodeError, TypeError):
                    parse_fail += 1

            counted = up + down + neutral + parse_fail
            passed = counted == total_feedback
            checks.append((
                "Feedback count consistency",
                passed,
                f"Counted: {counted}, Total: {total_feedback}" if not passed else f"OK ({up}👍 + {down}👎 + {neutral} neutral + {parse_fail} failed = {counted})"
            ))

        # Check 5: No duplicate chat IDs
        self.cursor.execute("""
            SELECT id, COUNT(*) as cnt FROM chat GROUP BY id HAVING cnt > 1
        """)
        dupes = self.cursor.fetchall()
        passed = len(dupes) == 0
        checks.append((
            "No duplicate chat IDs",
            passed,
            f"{len(dupes)} duplicate chat IDs found" if not passed else "OK"
        ))

        return checks

    def _get_schema_version(self) -> dict:
        """Detect database schema version and Open WebUI compatibility."""
        info = {
            'tables': [],
            'expected_tables': ['user', 'chat', 'feedback', 'auth', 'config'],
            'missing_tables': [],
            'extra_tables': [],
            'alembic_version': None
        }

        # Get all tables
        self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        info['tables'] = [row['name'] for row in self.cursor.fetchall()]

        # Check for expected tables
        info['missing_tables'] = [t for t in info['expected_tables'] if t not in info['tables']]

        # Get alembic version if exists
        if 'alembic_version' in info['tables']:
            self.cursor.execute("SELECT version_num FROM alembic_version LIMIT 1")
            row = self.cursor.fetchone()
            if row:
                info['alembic_version'] = row['version_num']

        # Check migrate history count
        if 'migratehistory' in info['tables']:
            self.cursor.execute("SELECT COUNT(*) as cnt FROM migratehistory")
            info['migration_count'] = self.cursor.fetchone()['cnt']

        return info

    def _report_data_quality(self):
        """Report parse success rates, sanity checks, and any data quality issues."""
        has_issues = self._parse_errors or self._unknown_ratings
        has_parse_stats = any(stats[1] > 0 for stats in self._parse_stats.values())

        # Always show parse success rates if we have stats
        if has_parse_stats:
            print("\n" + "=" * 60)
            print("📊 PARSE SUCCESS RATES")
            print("=" * 60)
            all_success = True
            for context, (success, total) in sorted(self._parse_stats.items()):
                if total > 0:
                    rate = success / total * 100
                    status = "✓" if rate == 100 else "⚠️" if rate >= 90 else "✗"
                    if rate < 100:
                        all_success = False
                    print(f"  {status} {context}: {success:,}/{total:,} ({rate:.1f}%)")
            if all_success:
                print("  All records parsed successfully!")

        # Run and report sanity checks
        checks = self._run_sanity_checks()
        failed_checks = [c for c in checks if not c[1]]

        print("\n" + "=" * 60)
        print("🔍 SANITY CHECKS")
        print("=" * 60)
        for name, passed, details in checks:
            status = "✓" if passed else "✗"
            print(f"  {status} {name}: {details}")

        if not failed_checks:
            print("  All sanity checks passed!")

        # Report warnings if any
        if has_issues:
            print("\n" + "=" * 60)
            print("⚠️  DATA QUALITY WARNINGS")
            print("=" * 60)

            if self._parse_errors:
                total_errors = sum(self._parse_errors.values())
                print(f"\nJSON Parse Errors: {total_errors:,} total")
                for context, count in sorted(self._parse_errors.items(), key=lambda x: -x[1]):
                    print(f"  - {context}: {count:,}")

            if self._unknown_ratings:
                total_unknown = sum(self._unknown_ratings.values())
                print(f"\nUnknown Rating Values: {total_unknown:,} total (not counted as 👍 or 👎)")
                for rating, count in sorted(self._unknown_ratings.items(), key=lambda x: -x[1]):
                    print(f"  - {rating}: {count:,}")

            print("\nUse 'verify' command for detailed investigation.")

        print()

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
        print(f"{'User':<40} {'Email':<30} {'Chats':>8}")
        print("-" * 80)
        for row in rows:
            name = row['name'] or 'Unknown'
            email = row['email'] or 'N/A'
            print(f"{name[:39]:<40} {email[:29]:<30} {row['chat_count']:>8,}")

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
                self._track_parse('chat messages', True)
            except (json.JSONDecodeError, TypeError) as e:
                self._track_error('chat_volume/messages', e)
                self._track_parse('chat messages', False)

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
        print(f"{'Name':<40} {'Role':<10} {'Chats':>6} {'Last Active':<20}")
        print("-" * 80)
        for row in self.cursor.fetchall():
            name = (row['name'] or 'Unknown')[:39]
            last_active = self._format_timestamp(row['last_active_at'])
            print(f"{name:<40} {row['role']:<10} {row['chat_count']:>6} {last_active:<20}")
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

    def usage_stats(self, min_chats: int = DEFAULT_MIN_CHATS, months: int = 9):
        """Analyze per-user per-month chat usage."""
        print("=" * 60)
        print("PER-USER MONTHLY USAGE")
        print("=" * 60)

        # Get all chats with user and timestamp
        self.cursor.execute("""
            SELECT c.user_id, c.id as chat_id, c.created_at, u.name, u.email
            FROM chat c
            LEFT JOIN user u ON c.user_id = u.id
        """)

        # Structure: user_id -> month -> count
        user_month_chats = defaultdict(lambda: defaultdict(int))
        user_total_chats = defaultdict(int)
        user_names = {}
        all_months = set()

        for row in self.cursor.fetchall():
            user_id = row['user_id']
            user_total_chats[user_id] += 1
            user_names[user_id] = row['name'] or row['email'] or user_id

            ts = row['created_at']
            if ts:
                dt = self._parse_timestamp(ts)
                if dt:
                    month_key = dt.strftime('%Y-%m')
                    user_month_chats[user_id][month_key] += 1
                    all_months.add(month_key)

        # Filter users by min_chats
        filtered_users = [u for u in user_total_chats.keys() if user_total_chats[u] >= min_chats]
        filtered_users.sort(key=lambda u: -user_total_chats[u])

        if not filtered_users:
            print(f"\nNo users with >= {min_chats} chats found.")
            print(f"Total users: {len(user_total_chats)}")
            return

        # Get last N months for display
        sorted_months = sorted(all_months)
        display_months = sorted_months[-months:]

        # Print header
        header = f"{'User':<35} {'Total':>6}"
        for m in display_months:
            header += f" {m[-5:]:>7}"
        print(f"\n{header}")
        print("-" * (43 + 8 * len(display_months)))

        # Print each user's monthly counts
        for user_id in filtered_users:
            name = user_names.get(user_id, '(unknown)')
            name = name[:34] if name else '(unknown)'
            total = user_total_chats[user_id]

            row_str = f"{name:<35} {total:>6}"
            for month in display_months:
                count = user_month_chats[user_id].get(month, 0)
                row_str += f" {count:>7}" if count > 0 else f" {'--':>7}"
            print(row_str)

        # Summary
        minor_users = len(user_total_chats) - len(filtered_users)
        if minor_users > 0:
            print(f"\n({minor_users} users with <{min_chats} chats not shown)")

        # Monthly totals row
        print("-" * (43 + 8 * len(display_months)))
        totals_row = f"{'TOTAL':<35} {sum(user_total_chats.values()):>6}"
        for month in display_months:
            month_total = sum(user_month_chats[u].get(month, 0) for u in user_total_chats.keys())
            totals_row += f" {month_total:>7}"
        print(totals_row)
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
                self._track_parse('model detection', True)
            except (json.JSONDecodeError, TypeError, IndexError) as e:
                model_counts['(parse error)'] += 1
                self._track_error('model_usage/chat', e)
                self._track_parse('model detection', False)

        print(f"\n{'Model':<50} {'Chats':>10}")
        print("-" * 62)
        for model, count in sorted(model_counts.items(), key=lambda x: -x[1]):
            print(f"{model[:49]:<50} {count:>10,}")
        print()

    def feedback_stats(self, min_chats: int = DEFAULT_MIN_CHATS):
        """Analyze thumbs up/down feedback statistics."""
        print("=" * 60)
        print("FEEDBACK ANALYSIS (Thumbs Up/Down)")
        print("=" * 60)

        # Check if feedback table exists
        self.cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='feedback'
        """)
        if not self.cursor.fetchone():
            print("\nNo feedback table found in database.")
            return

        # Total feedback count
        self.cursor.execute("SELECT COUNT(*) as count FROM feedback")
        total_feedback = self.cursor.fetchone()['count']
        print(f"\nTotal Feedback Entries: {total_feedback:,}")

        if total_feedback == 0:
            print("No feedback data to analyze.")
            return

        # Get total chat count for comparison
        self.cursor.execute("SELECT COUNT(*) as count FROM chat")
        total_chats = self.cursor.fetchone()['count']

        # Analyze feedback data
        self.cursor.execute("SELECT id, user_id, data, meta, created_at FROM feedback")

        thumbs_up = 0
        thumbs_down = 0
        neutral = 0
        by_model = defaultdict(lambda: {'up': 0, 'down': 0})
        by_user = defaultdict(lambda: {'up': 0, 'down': 0})
        monthly = defaultdict(lambda: {'up': 0, 'down': 0})
        chats_with_feedback = set()

        for row in self.cursor.fetchall():
            try:
                data = json.loads(row['data']) if row['data'] else {}
                meta = json.loads(row['meta']) if row['meta'] else {}

                # Track chat_id from meta
                chat_id = meta.get('chat_id')
                if chat_id:
                    chats_with_feedback.add(chat_id)

                # Extract rating - can be in different formats
                rating = data.get('rating')

                # Determine if positive or negative
                is_positive = False
                is_negative = False
                is_recognized = False

                if rating is not None:
                    if isinstance(rating, (int, float)):
                        is_recognized = True
                        if rating > 0:
                            is_positive = True
                        elif rating < 0:
                            is_negative = True
                        # rating == 0 is recognized but neutral
                    elif isinstance(rating, str):
                        rating_lower = rating.lower()
                        if rating_lower in ('1', 'like', 'positive', 'up', 'good', 'yes'):
                            is_positive = True
                            is_recognized = True
                        elif rating_lower in ('-1', 'dislike', 'negative', 'down', 'bad', 'no', '0'):
                            is_negative = True
                            is_recognized = True
                        else:
                            self._track_unknown_rating(rating)
                    else:
                        self._track_unknown_rating(rating)
                # rating is None is recognized as "no rating given"

                if is_positive:
                    thumbs_up += 1
                elif is_negative:
                    thumbs_down += 1
                else:
                    neutral += 1

                # Track by model
                model_id = data.get('model_id', '(unknown)')
                if is_positive:
                    by_model[model_id]['up'] += 1
                elif is_negative:
                    by_model[model_id]['down'] += 1

                # Track by user
                user_id = row['user_id'] or '(unknown)'
                if is_positive:
                    by_user[user_id]['up'] += 1
                elif is_negative:
                    by_user[user_id]['down'] += 1

                # Track by month
                ts = row['created_at']
                if ts:
                    dt = self._parse_timestamp(ts)
                    if dt:
                        month_key = dt.strftime('%Y-%m')
                        if is_positive:
                            monthly[month_key]['up'] += 1
                        elif is_negative:
                            monthly[month_key]['down'] += 1

                self._track_parse('feedback data', True)
            except (json.JSONDecodeError, TypeError) as e:
                self._track_error('feedback_stats/data', e)
                self._track_parse('feedback data', False)

        # Summary
        print(f"\n👍 Thumbs Up:   {thumbs_up:,}")
        print(f"👎 Thumbs Down: {thumbs_down:,}")
        if neutral > 0:
            print(f"➖ Neutral/Other: {neutral:,}")

        total_rated = thumbs_up + thumbs_down
        if total_rated > 0:
            satisfaction = (thumbs_up / total_rated) * 100
            print(f"\n📊 Satisfaction Rate: {satisfaction:.1f}%")

        # Chat feedback coverage
        chats_with_fb = len(chats_with_feedback)
        chats_without_fb = total_chats - chats_with_fb
        coverage_pct = (chats_with_fb / total_chats * 100) if total_chats > 0 else 0

        print("\n" + "-" * 40)
        print("CHAT FEEDBACK COVERAGE")
        print("-" * 40)
        print(f"Total Chats:              {total_chats:>8,}")
        print(f"Chats WITH feedback:      {chats_with_fb:>8,} ({coverage_pct:.1f}%)")
        print(f"Chats WITHOUT feedback:   {chats_without_fb:>8,} ({100-coverage_pct:.1f}%)")

        # By model
        if by_model:
            print("\n" + "-" * 50)
            print("FEEDBACK BY MODEL")
            print("-" * 50)
            print(f"{'Model':<35} {'👍':>6} {'👎':>6} {'Rate':>8}")
            print("-" * 57)
            for model, counts in sorted(by_model.items(), key=lambda x: -(x[1]['up'] + x[1]['down'])):
                total = counts['up'] + counts['down']
                rate = (counts['up'] / total * 100) if total > 0 else 0
                print(f"{model[:34]:<35} {counts['up']:>6} {counts['down']:>6} {rate:>7.1f}%")

        # By month - with no feedback tracking
        # First, get all chats grouped by month with their feedback status
        self.cursor.execute("SELECT id, created_at FROM chat")
        chats_by_month = defaultdict(lambda: {'total': 0, 'ids': []})
        for row in self.cursor.fetchall():
            ts = row['created_at']
            if ts:
                dt = self._parse_timestamp(ts)
                if dt:
                    month_key = dt.strftime('%Y-%m')
                    chats_by_month[month_key]['total'] += 1
                    chats_by_month[month_key]['ids'].append(row['id'])

        # Build feedback lookup: chat_id -> {'up': bool, 'down': bool}
        chat_feedback_type = {}
        self.cursor.execute("SELECT data, meta FROM feedback")
        for row in self.cursor.fetchall():
            try:
                data = json.loads(row['data']) if row['data'] else {}
                meta = json.loads(row['meta']) if row['meta'] else {}
                chat_id = meta.get('chat_id')
                if not chat_id:
                    continue

                rating = data.get('rating')
                is_positive = False
                is_negative = False

                if rating is not None:
                    if isinstance(rating, (int, float)):
                        is_positive = rating > 0
                        is_negative = rating < 0
                    elif isinstance(rating, str):
                        rating_lower = rating.lower()
                        is_positive = rating_lower in ('1', 'like', 'positive', 'up', 'good', 'yes')
                        is_negative = rating_lower in ('-1', 'dislike', 'negative', 'down', 'bad', 'no')

                if chat_id not in chat_feedback_type:
                    chat_feedback_type[chat_id] = {'up': False, 'down': False}
                if is_positive:
                    chat_feedback_type[chat_id]['up'] = True
                if is_negative:
                    chat_feedback_type[chat_id]['down'] = True
            except (json.JSONDecodeError, TypeError) as e:
                self._track_error('feedback_stats/chat_feedback_type', e)

        # Calculate monthly stats with no-feedback count
        print("\n" + "-" * 65)
        print("MONTHLY FEEDBACK COMPLIANCE")
        print("-" * 65)
        print(f"{'Month':<10} {'Chats':>7} {'No FB':>7} {'👍':>6} {'👎':>6} {'Rate':>8}")
        print("-" * 65)

        all_months = sorted(set(chats_by_month.keys()) | set(monthly.keys()))
        for month in all_months:
            month_chats = chats_by_month.get(month, {'total': 0, 'ids': []})
            total_month_chats = month_chats['total']

            # Count feedback types for this month's chats
            up_count = 0
            down_count = 0
            no_fb_count = 0

            for chat_id in month_chats['ids']:
                fb = chat_feedback_type.get(chat_id)
                if fb:
                    if fb['up']:
                        up_count += 1
                    if fb['down']:
                        down_count += 1
                    if not fb['up'] and not fb['down']:
                        no_fb_count += 1
                else:
                    no_fb_count += 1

            # Compliance rate = chats with any feedback / total chats
            with_fb = total_month_chats - no_fb_count
            compliance = (with_fb / total_month_chats * 100) if total_month_chats > 0 else 0
            print(f"{month:<10} {total_month_chats:>7} {no_fb_count:>7} {up_count:>6} {down_count:>6} {compliance:>7.1f}%")

        # User feedback compliance by month
        print("\n" + "-" * 75)
        print("USER FEEDBACK COMPLIANCE BY MONTH")
        print("-" * 75)

        # Get chats per user with month
        self.cursor.execute("""
            SELECT user_id, id as chat_id, created_at FROM chat
        """)
        # Structure: user_id -> month -> [chat_ids]
        user_month_chats = defaultdict(lambda: defaultdict(list))
        user_all_chats = defaultdict(list)
        all_user_months = set()

        for row in self.cursor.fetchall():
            user_id = row['user_id']
            chat_id = row['chat_id']
            user_all_chats[user_id].append(chat_id)

            ts = row['created_at']
            if ts:
                dt = self._parse_timestamp(ts)
                if dt:
                    month_key = dt.strftime('%Y-%m')
                    user_month_chats[user_id][month_key].append(chat_id)
                    all_user_months.add(month_key)

        # Get user names
        all_user_ids = list(user_all_chats.keys())
        user_names = {}
        if all_user_ids:
            placeholders = ','.join('?' * len(all_user_ids))
            self.cursor.execute(f"SELECT id, name, email FROM user WHERE id IN ({placeholders})", all_user_ids)
            for row in self.cursor.fetchall():
                user_names[row['id']] = row['name'] or row['email'] or row['id']

        # Sort months
        sorted_months = sorted(all_user_months)

        # Calculate overall compliance per user (for sorting)
        user_totals = {}
        for user_id, chat_ids in user_all_chats.items():
            total = len(chat_ids)
            no_fb = sum(1 for cid in chat_ids if not chat_feedback_type.get(cid) or
                       (not chat_feedback_type.get(cid, {}).get('up') and not chat_feedback_type.get(cid, {}).get('down')))
            user_totals[user_id] = {'total': total, 'no_fb': no_fb}

        # Sort users by total chats, filter to users with meaningful activity
        sorted_users = sorted(
            [u for u in user_all_chats.keys() if user_totals[u]['total'] >= min_chats],
            key=lambda u: -user_totals[u]['total']
        )

        # Print header with months (compact format: rate% up/dn)
        month_labels = sorted_months[-6:]  # Last 6 months
        header = f"{'User':<35} {'Tot':>4}"
        for m in month_labels:
            header += f" {m[-5:]:^13}"
        print(header)

        # Sub-header for columns
        sub_header = f"{'':<35} {'':>4}"
        for _ in month_labels:
            sub_header += f" {'Rate  👍/👎':^13}"
        print(sub_header)
        print("-" * (41 + 14 * len(month_labels)))

        # Print each user's monthly compliance rates with up/down
        # Show all users if 10 or fewer, otherwise top 10
        display_limit = len(sorted_users) if len(sorted_users) <= 10 else 10
        for user_id in sorted_users[:display_limit]:
            name = user_names.get(user_id, user_id or '(unknown)')
            name = name[:34] if name else '(unknown)'
            total = user_totals[user_id]['total']

            row_str = f"{name:<35} {total:>4}"

            for month in month_labels:
                month_chat_ids = user_month_chats[user_id].get(month, [])
                if not month_chat_ids:
                    row_str += f" {'--':^13}"
                else:
                    month_total = len(month_chat_ids)
                    month_up = 0
                    month_down = 0
                    month_no_fb = 0

                    for cid in month_chat_ids:
                        fb = chat_feedback_type.get(cid)
                        if fb:
                            if fb['up']:
                                month_up += 1
                            if fb['down']:
                                month_down += 1
                            if not fb['up'] and not fb['down']:
                                month_no_fb += 1
                        else:
                            month_no_fb += 1

                    month_rate = ((month_total - month_no_fb) / month_total * 100) if month_total > 0 else 0
                    cell = f"{month_rate:3.0f}% {month_up:>2}/{month_down:<2}"
                    row_str += f" {cell:^13}"

            print(row_str)

        # Note about filter
        minor_user_count = len(user_all_chats) - len(sorted_users)
        filter_note = f", {minor_user_count} users with <{min_chats} chats not shown" if minor_user_count > 0 else ""
        print(f"\n(Rate = compliance %, 👍/👎 = thumbs up/down counts, '--' = no chats{filter_note})")

        # Thumbs up/down counter per month per user (dedicated view)
        print("\n" + "-" * 75)
        print("THUMBS UP/DOWN COUNT PER MONTH PER USER")
        print("-" * 75)

        # Get last 6 months for display
        display_months = sorted_months[-6:]

        # Build header with months (each month gets 👍 and 👎 columns)
        header = f"{'User':<30}"
        for m in display_months:
            header += f" {m[-5:]:^11}"
        print(header)

        # Sub-header for up/down columns
        sub_header = f"{'':<30}"
        for _ in display_months:
            sub_header += f" {'👍':>5} {'👎':>4}"
        print(sub_header)
        print("-" * (30 + 12 * len(display_months)))

        # Print each user's monthly up/down counts
        display_limit = len(sorted_users) if len(sorted_users) <= 15 else 15
        for user_id in sorted_users[:display_limit]:
            name = user_names.get(user_id, user_id or '(unknown)')
            name = name[:29] if name else '(unknown)'

            row_str = f"{name:<30}"

            for month in display_months:
                month_chat_ids = user_month_chats[user_id].get(month, [])
                if not month_chat_ids:
                    row_str += f" {'--':>5} {'--':>4}"
                else:
                    month_up = 0
                    month_down = 0

                    for cid in month_chat_ids:
                        fb = chat_feedback_type.get(cid)
                        if fb:
                            if fb['up']:
                                month_up += 1
                            if fb['down']:
                                month_down += 1

                    row_str += f" {month_up:>5} {month_down:>4}"

            print(row_str)

        # Totals row
        print("-" * (30 + 12 * len(display_months)))
        totals_row = f"{'TOTAL':<30}"
        for month in display_months:
            month_up_total = 0
            month_down_total = 0
            for user_id in user_all_chats.keys():
                for cid in user_month_chats[user_id].get(month, []):
                    fb = chat_feedback_type.get(cid)
                    if fb:
                        if fb['up']:
                            month_up_total += 1
                        if fb['down']:
                            month_down_total += 1
            totals_row += f" {month_up_total:>5} {month_down_total:>4}"
        print(totals_row)

        if minor_user_count > 0:
            print(f"\n({minor_user_count} users with <{min_chats} chats not shown)")

        # Also show summary table
        print("\n" + "-" * 75)
        print("USER FEEDBACK SUMMARY (All Time)")
        print("-" * 75)

        user_compliance = []
        for user_id, chat_ids in user_all_chats.items():
            total_user_chats = len(chat_ids)
            up_count = 0
            down_count = 0
            no_fb_count = 0

            for chat_id in chat_ids:
                fb = chat_feedback_type.get(chat_id)
                if fb:
                    if fb['up']:
                        up_count += 1
                    if fb['down']:
                        down_count += 1
                    if not fb['up'] and not fb['down']:
                        no_fb_count += 1
                else:
                    no_fb_count += 1

            with_fb = total_user_chats - no_fb_count
            compliance = (with_fb / total_user_chats * 100) if total_user_chats > 0 else 0
            user_compliance.append({
                'user_id': user_id,
                'name': user_names.get(user_id, user_id or '(unknown)'),
                'total': total_user_chats,
                'no_fb': no_fb_count,
                'up': up_count,
                'down': down_count,
                'rate': compliance
            })

        # Filter to users with min_chats+ chats and sort
        all_user_count = len(user_compliance)
        user_compliance = [u for u in user_compliance if u['total'] >= min_chats]
        user_compliance.sort(key=lambda x: -x['total'])

        print(f"{'User':<40} {'Chats':>7} {'No FB':>7} {'👍':>6} {'👎':>6} {'Rate':>8}")
        print("-" * 90)
        for u in user_compliance:
            name = u['name'][:39] if u['name'] else '(unknown)'
            print(f"{name:<40} {u['total']:>7} {u['no_fb']:>7} {u['up']:>6} {u['down']:>6} {u['rate']:>7.1f}%")

        # Show count of minor users
        minor_users = all_user_count - len(user_compliance)
        if minor_users > 0:
            print(f"\n({minor_users} users with <{min_chats} chats not shown)")

        print()

    def report(self, month: str = None, history_months: int = 6):
        """Generate concise report for Simon (MC Weekly / ITG Meeting).

        Args:
            month: Target month in YYYY-MM format. Defaults to last complete month.
            history_months: Number of months of history to show (default: 6).
        """
        from datetime import date

        # Determine target month
        if month:
            target_month = month
        else:
            # Default to last complete month
            today = date.today()
            if today.month == 1:
                target_month = f"{today.year - 1}-12"
            else:
                target_month = f"{today.year}-{today.month - 1:02d}"

        print("=" * 60)
        print(f"AGENT-ASSIST REPORT")
        print("=" * 60)

        # Get user names
        self.cursor.execute("SELECT id, name, email FROM user")
        user_names = {}
        for row in self.cursor.fetchall():
            user_names[row['id']] = row['name'] or row['email'] or row['id']

        # Collect all feedback by month and user
        monthly_stats = defaultdict(lambda: {'up': 0, 'down': 0})
        user_monthly_stats = defaultdict(lambda: defaultdict(lambda: {'up': 0, 'down': 0}))

        self.cursor.execute("SELECT user_id, data, created_at FROM feedback")

        for row in self.cursor.fetchall():
            ts = row['created_at']
            if not ts:
                continue
            dt = self._parse_timestamp(ts)
            if not dt:
                continue

            row_month = dt.strftime('%Y-%m')

            try:
                data = json.loads(row['data']) if row['data'] else {}
                rating = data.get('rating')
                user_id = row['user_id']

                is_up = False
                is_down = False

                if isinstance(rating, (int, float)):
                    if rating > 0:
                        is_up = True
                    elif rating < 0:
                        is_down = True
                elif isinstance(rating, str):
                    rating_lower = rating.lower()
                    if rating_lower in ('1', 'like', 'positive', 'up', 'good', 'yes'):
                        is_up = True
                    elif rating_lower in ('-1', 'dislike', 'negative', 'down', 'bad', 'no'):
                        is_down = True

                if is_up:
                    monthly_stats[row_month]['up'] += 1
                    user_monthly_stats[user_id][row_month]['up'] += 1
                elif is_down:
                    monthly_stats[row_month]['down'] += 1
                    user_monthly_stats[user_id][row_month]['down'] += 1
            except (json.JSONDecodeError, TypeError):
                pass

        # Get sorted months, last N
        all_months = sorted(monthly_stats.keys())
        display_months = all_months[-history_months:]

        # Monthly summary table
        print(f"\n{'Month':<10} {'Usage':>8} {'👍':>6} {'👎':>5} {'Accuracy':>10}")
        print("-" * 45)
        for m in display_months:
            stats = monthly_stats[m]
            total = stats['up'] + stats['down']
            acc = (stats['up'] / total * 100) if total > 0 else 0
            print(f"{m:<10} {total:>8,} {stats['up']:>6,} {stats['down']:>5,} {acc:>9.1f}%")

        # Highlight latest month
        if display_months:
            latest = display_months[-1]
            latest_stats = monthly_stats[latest]
            latest_total = latest_stats['up'] + latest_stats['down']
            latest_acc = (latest_stats['up'] / latest_total * 100) if latest_total > 0 else 0

            # Find first month for comparison
            first = display_months[0]
            first_stats = monthly_stats[first]
            first_total = first_stats['up'] + first_stats['down']
            first_acc = (first_stats['up'] / first_total * 100) if first_total > 0 else 0

            print("-" * 45)
            print(f"\n📊 Summary ({first} → {latest}):")
            print(f"   Usage:    {first_total:,} → {latest_total:,}")
            print(f"   Accuracy: {first_acc:.0f}% → {latest_acc:.0f}%")

        # Per-user breakdown for latest month
        if display_months:
            latest = display_months[-1]
            print(f"\n{'User':<20} ", end="")
            for m in display_months[-4:]:  # Last 4 months
                print(f"{m[-5:]:>12}", end="")
            print()
            print("-" * (20 + 12 * min(4, len(display_months))))

            # Get users with activity in latest month
            active_users = []
            for user_id, months_data in user_monthly_stats.items():
                if latest in months_data:
                    latest_total = months_data[latest]['up'] + months_data[latest]['down']
                    active_users.append((user_id, latest_total))

            # Sort by latest month usage
            active_users.sort(key=lambda x: -x[1])

            for user_id, _ in active_users:
                name = user_names.get(user_id, user_id or '(unknown)')
                name = name[:19] if name else '(unknown)'
                print(f"{name:<20} ", end="")

                for m in display_months[-4:]:
                    stats = user_monthly_stats[user_id].get(m, {'up': 0, 'down': 0})
                    total = stats['up'] + stats['down']
                    if total > 0:
                        acc = stats['up'] / total * 100
                        print(f"{total:>5} ({acc:>4.0f}%)", end="")
                    else:
                        print(f"{'--':>12}", end="")
                print()

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

    def recent_changes(self, days: int = 7):
        """Show recent config changes across models, knowledge, functions, tools, and files."""
        print("=" * 70)
        print(f"RECENT CONFIG CHANGES (Last {days} days)")
        print("=" * 70)

        # Calculate cutoff - we'll compare after parsing timestamps
        from time import time
        cutoff_time = time() - days * 86400
        cutoff_dt = datetime.fromtimestamp(cutoff_time)

        # Tables to check: (table_name, name_column)
        config_tables = [
            ('model', 'name'),
            ('knowledge', 'name'),
            ('function', 'name'),
            ('tool', 'name'),
            ('file', 'filename'),
        ]

        all_changes = []

        for table, name_col in config_tables:
            # Check if table exists
            self.cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)
            )
            if not self.cursor.fetchone():
                continue

            # Check if updated_at column exists
            self.cursor.execute(f"PRAGMA table_info([{table}])")
            columns = [row['name'] for row in self.cursor.fetchall()]

            if 'updated_at' not in columns:
                continue

            # Query all records and filter in Python (handles various timestamp formats)
            query = f"""
                SELECT id, {name_col} as name, created_at, updated_at
                FROM [{table}]
                ORDER BY updated_at DESC
            """
            self.cursor.execute(query)

            for row in self.cursor.fetchall():
                created = self._parse_timestamp(row['created_at'])
                updated = self._parse_timestamp(row['updated_at'])

                # Skip if no valid timestamp or outside date range
                if not updated or updated < cutoff_dt:
                    continue

                # Determine if this is a create or update
                is_new = False
                if created and updated:
                    # If created and updated are within 1 minute, it's a new record
                    is_new = abs((updated - created).total_seconds()) < 60

                all_changes.append({
                    'type': table,
                    'id': row['id'],
                    'name': row['name'] or '(unnamed)',
                    'updated_at': row['updated_at'],
                    'updated_dt': updated,
                    'action': 'Created' if is_new else 'Modified'
                })

        # Sort by updated datetime descending
        all_changes.sort(key=lambda x: x['updated_dt'], reverse=True)

        if not all_changes:
            print(f"\nNo config changes found in the last {days} days.")
            print(f"\nTables checked: model, knowledge, function, tool, file")
            print(f"Cutoff date: {cutoff_dt.strftime('%Y-%m-%d %H:%M')}")

            # Debug: show sample timestamps from each table
            if self.debug:
                print("\n[DEBUG] Sample timestamps from tables:")
                for table, name_col in config_tables:
                    self.cursor.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)
                    )
                    if not self.cursor.fetchone():
                        continue
                    self.cursor.execute(f"SELECT updated_at FROM [{table}] LIMIT 3")
                    rows = self.cursor.fetchall()
                    if rows:
                        print(f"  {table}: {[row['updated_at'] for row in rows]}")
                        for row in rows:
                            parsed = self._parse_timestamp(row['updated_at'])
                            print(f"    -> parsed: {parsed}")
            return

        print(f"\nFound {len(all_changes)} change(s):\n")
        print(f"{'Type':<12} {'Action':<10} {'Name':<35} {'When':<20}")
        print("-" * 80)

        for change in all_changes:
            when = change['updated_dt'].strftime('%Y-%m-%d %H:%M') if change['updated_dt'] else 'N/A'
            name = (change['name'] or '(unnamed)')[:34]
            print(f"{change['type']:<12} {change['action']:<10} {name:<35} {when:<20}")

        # Summary by type
        print("\n" + "-" * 40)
        print("SUMMARY BY TYPE")
        print("-" * 40)
        from collections import Counter
        type_counts = Counter(c['type'] for c in all_changes)
        for t, count in type_counts.most_common():
            print(f"  {t}: {count}")

        print()

    def verify(self):
        """Verify data accuracy with cross-checks and sample data."""
        print("=" * 70)
        print("DATA VERIFICATION")
        print("=" * 70)

        # 1. Direct SQL counts
        print("\n1. RAW TABLE COUNTS (Direct SQL)")
        print("-" * 50)

        self.cursor.execute("SELECT COUNT(*) as count FROM chat")
        chat_count = self.cursor.fetchone()['count']
        print(f"   Total chats in 'chat' table:     {chat_count:,}")

        self.cursor.execute("SELECT COUNT(*) as count FROM user")
        user_count = self.cursor.fetchone()['count']
        print(f"   Total users in 'user' table:     {user_count:,}")

        self.cursor.execute("SELECT COUNT(*) as count FROM feedback")
        feedback_count = self.cursor.fetchone()['count']
        print(f"   Total rows in 'feedback' table:  {feedback_count:,}")

        # 2. Feedback rating distribution (raw)
        print("\n2. FEEDBACK RATING VALUES (Raw from database)")
        print("-" * 50)

        self.cursor.execute("SELECT data FROM feedback LIMIT 100")
        rating_values = {}
        for row in self.cursor.fetchall():
            try:
                data = json.loads(row['data']) if row['data'] else {}
                rating = data.get('rating')
                rating_key = f"{type(rating).__name__}:{rating}"
                rating_values[rating_key] = rating_values.get(rating_key, 0) + 1
            except (json.JSONDecodeError, TypeError):
                rating_values['(parse error)'] = rating_values.get('(parse error)', 0) + 1

        print("   Rating values found (type:value -> count):")
        for key, count in sorted(rating_values.items(), key=lambda x: -x[1]):
            print(f"      {key}: {count}")

        # 3. Sample feedback records
        print("\n3. SAMPLE FEEDBACK RECORDS (First 5)")
        print("-" * 50)

        self.cursor.execute("""
            SELECT f.id, f.user_id, f.data, f.meta, f.created_at, u.name as user_name
            FROM feedback f
            LEFT JOIN user u ON f.user_id = u.id
            ORDER BY f.created_at DESC
            LIMIT 5
        """)

        for i, row in enumerate(self.cursor.fetchall(), 1):
            print(f"\n   Record {i}:")
            print(f"      ID: {row['id'][:20]}...")
            print(f"      User: {row['user_name'] or row['user_id']}")
            print(f"      Created: {self._format_timestamp(row['created_at'])}")

            try:
                data = json.loads(row['data']) if row['data'] else {}
                meta = json.loads(row['meta']) if row['meta'] else {}
                print(f"      Rating: {data.get('rating')} (type: {type(data.get('rating')).__name__})")
                print(f"      Model: {data.get('model_id', 'N/A')}")
                print(f"      Chat ID: {meta.get('chat_id', 'N/A')[:20] if meta.get('chat_id') else 'N/A'}...")
            except (json.JSONDecodeError, TypeError) as e:
                print(f"      (Error parsing: {e})")

        # 4. Cross-check: chats with feedback
        print("\n4. CROSS-CHECK: Chats with Feedback")
        print("-" * 50)

        self.cursor.execute("SELECT meta FROM feedback")
        chat_ids_with_feedback = set()
        for row in self.cursor.fetchall():
            try:
                meta = json.loads(row['meta']) if row['meta'] else {}
                chat_id = meta.get('chat_id')
                if chat_id:
                    chat_ids_with_feedback.add(chat_id)
            except (json.JSONDecodeError, TypeError):
                pass

        # Verify these chat_ids exist in chat table
        if chat_ids_with_feedback:
            placeholders = ','.join('?' * min(100, len(chat_ids_with_feedback)))
            sample_ids = list(chat_ids_with_feedback)[:100]
            self.cursor.execute(f"SELECT COUNT(*) as count FROM chat WHERE id IN ({placeholders})", sample_ids)
            existing = self.cursor.fetchone()['count']
            print(f"   Unique chat IDs in feedback meta: {len(chat_ids_with_feedback):,}")
            print(f"   Sample of {len(sample_ids)} verified in chat table: {existing} exist")
            if existing != len(sample_ids):
                print(f"   ⚠️  {len(sample_ids) - existing} chat IDs in feedback don't exist in chat table")
        else:
            print("   No chat IDs found in feedback meta")

        chats_without_fb = chat_count - len(chat_ids_with_feedback)
        print(f"   Chats without any feedback: {chats_without_fb:,}")

        # 5. Consistency check
        print("\n5. CONSISTENCY CHECK")
        print("-" * 50)

        self.cursor.execute("SELECT data FROM feedback")
        calc_up = 0
        calc_down = 0
        calc_other = 0

        for row in self.cursor.fetchall():
            try:
                data = json.loads(row['data']) if row['data'] else {}
                rating = data.get('rating')

                if rating is not None:
                    if isinstance(rating, (int, float)):
                        if rating > 0:
                            calc_up += 1
                        elif rating < 0:
                            calc_down += 1
                        else:
                            calc_other += 1
                    elif isinstance(rating, str):
                        rating_lower = rating.lower()
                        if rating_lower in ('1', 'like', 'positive', 'up', 'good', 'yes'):
                            calc_up += 1
                        elif rating_lower in ('-1', 'dislike', 'negative', 'down', 'bad', 'no'):
                            calc_down += 1
                        else:
                            calc_other += 1
                    else:
                        calc_other += 1
                else:
                    calc_other += 1
            except (json.JSONDecodeError, TypeError):
                calc_other += 1

        total_calc = calc_up + calc_down + calc_other
        print(f"   Calculated 👍 (positive): {calc_up:,}")
        print(f"   Calculated 👎 (negative): {calc_down:,}")
        print(f"   Other/neutral/null:       {calc_other:,}")
        print(f"   Total:                    {total_calc:,}")

        if total_calc == feedback_count:
            print(f"   ✓ Total matches feedback table count ({feedback_count:,})")
        else:
            print(f"   ⚠️  Mismatch! Expected {feedback_count:,}, got {total_calc:,}")

        print("\n" + "=" * 70)
        print("Verification complete. Review sample data to confirm rating parsing.")
        print("=" * 70)

    def plot_trends(self, output_path: str = None, include_current_month: bool = False,
                    start_month: str = None):
        """Generate a dual-axis chart showing monthly usage and accuracy trends.

        Args:
            output_path: Path to save PNG. Defaults to 'chatbot_trends.png'.
            include_current_month: If False (default), excludes the current incomplete month.
            start_month: Only include months >= this (format: YYYY-MM). Default: 2025-08.
        """
        # Default to Aug 2025 (pilot start)
        if start_month is None:
            start_month = '2025-08'
        try:
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            try:
                import seaborn as sns
                sns.set_theme(style="whitegrid", palette="muted")
                has_seaborn = True
            except ImportError:
                has_seaborn = False
        except ImportError:
            print("Error: matplotlib required for plotting. Install with: pip install matplotlib seaborn")
            return

        print("=" * 60)
        print("GENERATING TREND CHART")
        print("=" * 60)

        # Get current month to exclude if needed
        current_month = datetime.now().strftime('%Y-%m')

        # Collect monthly stats from TWO sources:
        # 1. Total conversations from chat table (for bars)
        # 2. Accuracy from feedback table (for line)

        monthly_stats = defaultdict(lambda: {'chats': 0, 'up': 0, 'down': 0})

        # Count all chats per month
        self.cursor.execute("SELECT created_at FROM chat")
        for row in self.cursor.fetchall():
            ts = row['created_at']
            if not ts:
                continue
            dt = self._parse_timestamp(ts)
            if dt:
                month_key = dt.strftime('%Y-%m')
                monthly_stats[month_key]['chats'] += 1

        # Count feedback (thumbs up/down) per month for accuracy
        self.cursor.execute("SELECT data, created_at FROM feedback")
        for row in self.cursor.fetchall():
            ts = row['created_at']
            if not ts:
                continue
            dt = self._parse_timestamp(ts)
            if not dt:
                continue

            month_key = dt.strftime('%Y-%m')

            try:
                data = json.loads(row['data']) if row['data'] else {}
                rating = data.get('rating')

                if isinstance(rating, (int, float)):
                    if rating > 0:
                        monthly_stats[month_key]['up'] += 1
                    elif rating < 0:
                        monthly_stats[month_key]['down'] += 1
                elif isinstance(rating, str):
                    rating_lower = rating.lower()
                    if rating_lower in ('1', 'like', 'positive', 'up', 'good', 'yes'):
                        monthly_stats[month_key]['up'] += 1
                    elif rating_lower in ('-1', 'dislike', 'negative', 'down', 'bad', 'no'):
                        monthly_stats[month_key]['down'] += 1
            except (json.JSONDecodeError, TypeError):
                pass

        if not monthly_stats:
            print("No data to plot.")
            return

        # Exclude current incomplete month unless explicitly included
        if not include_current_month and current_month in monthly_stats:
            del monthly_stats[current_month]
            print(f"(Excluding current month {current_month} - use --include-current to include)")

        # Filter to start_month onwards
        months_to_remove = [m for m in monthly_stats.keys() if m < start_month]
        for m in months_to_remove:
            del monthly_stats[m]
        if months_to_remove:
            print(f"(Showing {start_month} onwards - use --start-month to change)")

        if not monthly_stats:
            print("No complete months to plot.")
            return

        # Prepare data for plotting
        months = sorted(monthly_stats.keys())
        chats = [monthly_stats[m]['chats'] for m in months]
        accuracy = []
        for m in months:
            total_rated = monthly_stats[m]['up'] + monthly_stats[m]['down']
            acc = (monthly_stats[m]['up'] / total_rated * 100) if total_rated > 0 else 0
            accuracy.append(acc)

        # Convert month strings to datetime for better x-axis formatting
        month_dates = [datetime.strptime(m, '%Y-%m') for m in months]

        # Professional color scheme (per data viz best practices)
        # Teal bars + Coral line - high contrast, colorblind safe, distinct
        COLOR_BARS = '#0077B6'   # Teal/Blue
        COLOR_LINE = '#E63946'   # Coral/Red
        COLOR_GRID = '#E8E8E8'   # Light grey

        # Set clean style
        if has_seaborn:
            sns.set_style("white")

        # Create figure with dual y-axes
        fig, ax1 = plt.subplots(figsize=(11, 5))

        # === LEFT AXIS: Conversation bars ===
        ax1.set_ylabel('Conversations', color=COLOR_BARS, fontsize=11, fontweight='semibold')
        bars = ax1.bar(month_dates, chats, width=18, color=COLOR_BARS, alpha=0.85,
                      edgecolor='white', linewidth=0.8)
        ax1.tick_params(axis='y', labelcolor=COLOR_BARS, labelsize=10)
        ax1.set_ylim(0, max(chats) * 1.25 if chats else 100)

        # Value labels on bars (above)
        max_chats = max(chats) if chats else 1
        for bar, val in zip(bars, chats):
            ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max_chats*0.02,
                    f'{val:,}', ha='center', va='bottom', fontsize=9, color=COLOR_BARS,
                    fontweight='semibold')

        # === RIGHT AXIS: Accuracy line ===
        ax2 = ax1.twinx()
        ax2.set_ylabel('Accuracy (%)', color=COLOR_LINE, fontsize=11, fontweight='semibold')
        ax2.plot(month_dates, accuracy, color=COLOR_LINE, linewidth=2.5, marker='o',
                markersize=9, markerfacecolor='white', markeredgecolor=COLOR_LINE,
                markeredgewidth=2.5, zorder=5)
        ax2.tick_params(axis='y', labelcolor=COLOR_LINE, labelsize=10)
        ax2.set_ylim(0, 105)

        # Value labels on line - only first, last, and notable changes
        # Use background box to prevent overlap with bars
        for i, (x, y) in enumerate(zip(month_dates, accuracy)):
            is_first = (i == 0)
            is_last = (i == len(accuracy) - 1)
            is_notable = (i > 0 and abs(y - accuracy[i-1]) > 5)

            if is_first or is_last or is_notable:
                # Place label below line if accuracy > 85% to avoid top crowding
                offset_y = -15 if y > 85 else 12
                va = 'top' if y > 85 else 'bottom'
                ax2.annotate(f'{y:.0f}%', (x, y), textcoords="offset points",
                            xytext=(0, offset_y), ha='center', va=va,
                            fontsize=9, color=COLOR_LINE, fontweight='bold',
                            bbox=dict(boxstyle='round,pad=0.2', facecolor='white',
                                     edgecolor='none', alpha=0.8))

        # === STYLING: Minimal, professional ===
        # Horizontal gridlines only (behind data)
        ax1.grid(axis='y', color=COLOR_GRID, linewidth=0.5, zorder=0)
        ax1.set_axisbelow(True)

        # Remove all spines except bottom
        for spine in ['top', 'right']:
            ax1.spines[spine].set_visible(False)
        ax1.spines['left'].set_color(COLOR_BARS)
        ax1.spines['left'].set_linewidth(1.5)
        ax2.spines['top'].set_visible(False)
        ax2.spines['right'].set_color(COLOR_LINE)
        ax2.spines['right'].set_linewidth(1.5)
        ax2.spines['left'].set_visible(False)

        # X-axis formatting
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
        ax1.xaxis.set_major_locator(mdates.MonthLocator())
        ax1.tick_params(axis='x', labelsize=10)
        plt.xticks(rotation=0)  # Keep horizontal for readability

        # Title
        ax1.set_title('Agent-Assist Chatbot: Conversations & Accuracy',
                     fontsize=13, fontweight='bold', pad=15, loc='left')

        # No legend needed - axis colors indicate which is which

        plt.tight_layout()

        # Save
        if output_path is None:
            output_path = 'chatbot_trends.png'

        plt.savefig(output_path, dpi=200, bbox_inches='tight', facecolor='white',
                   edgecolor='none')
        print(f"\n✓ Chart saved to: {output_path}")

        # Also print the data table
        print(f"\n{'Month':<10} {'Chats':>8} {'👍':>6} {'👎':>5} {'Accuracy':>10}")
        print("-" * 45)
        for m in months:
            stats = monthly_stats[m]
            total_rated = stats['up'] + stats['down']
            acc = (stats['up'] / total_rated * 100) if total_rated > 0 else 0
            print(f"{m:<10} {stats['chats']:>8,} {stats['up']:>6,} {stats['down']:>5,} {acc:>9.1f}%")

        print()

    def compare_export(self, export_path: str):
        """Compare database analysis against Open WebUI JSON export for verification."""
        print("=" * 70)
        print("VERIFICATION: Database vs Open WebUI Export")
        print("=" * 70)

        # Load exported JSON
        try:
            with open(export_path, 'r', encoding='utf-8') as f:
                export_data = json.load(f)
        except FileNotFoundError:
            print(f"Error: Export file not found: {export_path}")
            return
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in export file: {e}")
            return

        if not isinstance(export_data, list):
            print("Error: Export file should contain a JSON array")
            return

        print(f"\nExport file: {export_path}")
        print(f"Records in export: {len(export_data):,}")

        # Count from export (using data.rating: 1 = up, -1 = down)
        export_up = 0
        export_down = 0
        export_other = 0
        export_chat_ids = set()

        for record in export_data:
            data = record.get('data', {})
            meta = record.get('meta', {})
            rating = data.get('rating')

            if rating == 1:
                export_up += 1
            elif rating == -1:
                export_down += 1
            else:
                export_other += 1

            chat_id = meta.get('chat_id')
            if chat_id:
                export_chat_ids.add(chat_id)

        # Count from database
        self.cursor.execute("SELECT COUNT(*) as count FROM feedback")
        db_total = self.cursor.fetchone()['count']

        self.cursor.execute("SELECT data, meta FROM feedback")
        db_up = 0
        db_down = 0
        db_other = 0
        db_chat_ids = set()

        for row in self.cursor.fetchall():
            try:
                data = json.loads(row['data']) if row['data'] else {}
                meta = json.loads(row['meta']) if row['meta'] else {}
                rating = data.get('rating')

                # Match export logic: 1 = up, -1 = down
                if rating == 1:
                    db_up += 1
                elif rating == -1:
                    db_down += 1
                else:
                    db_other += 1

                chat_id = meta.get('chat_id')
                if chat_id:
                    db_chat_ids.add(chat_id)
            except (json.JSONDecodeError, TypeError):
                db_other += 1

        # Comparison table
        print("\n" + "-" * 70)
        print("COMPARISON")
        print("-" * 70)
        print(f"{'Metric':<30} {'Export':>12} {'Database':>12} {'Match':>10}")
        print("-" * 70)

        def check(name, exp_val, db_val):
            match = "✓" if exp_val == db_val else f"✗ (diff: {db_val - exp_val:+d})"
            print(f"{name:<30} {exp_val:>12,} {db_val:>12,} {match:>10}")
            return exp_val == db_val

        all_match = True
        all_match &= check("Total records", len(export_data), db_total)
        all_match &= check("Thumbs up (rating=1)", export_up, db_up)
        all_match &= check("Thumbs down (rating=-1)", export_down, db_down)
        all_match &= check("Other/null ratings", export_other, db_other)
        all_match &= check("Unique chat IDs", len(export_chat_ids), len(db_chat_ids))

        print("-" * 70)

        if all_match:
            print("\n✓ ALL METRICS MATCH - Database analysis is accurate!")
        else:
            print("\n⚠️  SOME METRICS DON'T MATCH - Review differences above")
            print("\nPossible reasons for mismatch:")
            print("  - Export was filtered by date range")
            print("  - Database has newer records since export")
            print("  - Export is from a different database")

        # Show any IDs in export but not in database
        missing_in_db = export_chat_ids - db_chat_ids
        if missing_in_db:
            print(f"\n⚠️  {len(missing_in_db)} chat IDs in export not found in database feedback")

        extra_in_db = db_chat_ids - export_chat_ids
        if extra_in_db:
            print(f"\n📝 {len(extra_in_db)} chat IDs in database not in export (newer records?)")

        print()

    def _parse_timestamp(self, ts) -> datetime | None:
        """Parse timestamp (could be seconds, milliseconds, or nanoseconds)."""
        if not ts:
            return None
        try:
            # Detect timestamp format based on magnitude:
            # - Current timestamps in seconds: ~1.7e9 (2024)
            # - In milliseconds: ~1.7e12
            # - In nanoseconds: ~1.7e18
            if ts > 1e15:  # Nanoseconds (> quadrillion)
                ts = ts / 1e9
            elif ts > 1e11:  # Milliseconds (> 100 billion)
                ts = ts / 1e3
            # else: already in seconds
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError, OverflowError):
            return None

    def _format_timestamp(self, ts) -> str:
        """Format timestamp for display."""
        dt = self._parse_timestamp(ts)
        return dt.strftime('%Y-%m-%d %H:%M') if dt else 'N/A'


def main():
    parser = argparse.ArgumentParser(
        description='Open WebUI Database Analyzer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  summary   - Overview of all tables and record counts (default)
  chats     - Chat volume analysis
  users     - User statistics
  timeline  - Chat activity over time
  usage     - Per-user per-month chat counts
  models    - Model usage statistics
  feedback  - Thumbs up/down feedback statistics
  plot      - Generate trend chart (usage + accuracy over time)
  report    - Concise report for Simon (usage + accuracy by month)
  changes   - Recent config changes (models, knowledge, functions, tools, files)
  verify    - Verify data accuracy with cross-checks
  compare   - Compare DB against Open WebUI JSON export
  export    - Export chat data to JSON
  all       - Run all analyses
"""
    )
    parser.add_argument('db_path', help='Path to webui.db file')
    parser.add_argument('command', nargs='?', default='summary',
                        choices=['summary', 'chats', 'users', 'timeline', 'usage', 'models', 'feedback', 'plot', 'report', 'changes', 'verify', 'compare', 'export', 'all'],
                        help='Command to run (default: summary)')
    parser.add_argument('--all-users', '-a', action='store_true',
                        help='Show all users (default: hide users with <500 chats)')
    parser.add_argument('--min-chats', '-m', type=int, default=DEFAULT_MIN_CHATS,
                        help=f'Minimum chats to show user (default: {DEFAULT_MIN_CHATS})')
    parser.add_argument('--days', type=int, default=7,
                        help='Days to look back for changes command (default: 7)')
    parser.add_argument('--months', type=int, default=9,
                        help='Months to display for usage command (default: 9)')
    parser.add_argument('--export-file', '-e', help='Open WebUI feedback JSON export (for compare command)')
    parser.add_argument('--output', '-o', help='Output file for export command')
    parser.add_argument('--month', help='Target month for report command (YYYY-MM, default: last complete month)')
    parser.add_argument('--debug', '-d', action='store_true',
                        help='Show debug info for parse errors and unknown values')
    parser.add_argument('--include-current', action='store_true',
                        help='Include current (incomplete) month in plot (default: exclude)')
    parser.add_argument('--start-month', default='2025-08',
                        help='Start month for plot (YYYY-MM, default: 2025-08)')

    args = parser.parse_args()

    # Determine min_chats threshold
    min_chats = 0 if args.all_users else args.min_chats

    try:
        with OpenWebUIAnalyzer(args.db_path, debug=args.debug) as analyzer:
            if args.command == 'summary':
                analyzer.summary()
                analyzer.chat_volume()
            elif args.command == 'chats':
                analyzer.chat_volume()
            elif args.command == 'users':
                analyzer.user_stats()
            elif args.command == 'timeline':
                analyzer.timeline()
            elif args.command == 'usage':
                analyzer.usage_stats(min_chats=min_chats, months=args.months)
            elif args.command == 'models':
                analyzer.model_usage()
            elif args.command == 'feedback':
                analyzer.feedback_stats(min_chats=min_chats)
            elif args.command == 'plot':
                analyzer.plot_trends(args.output, include_current_month=args.include_current,
                                    start_month=args.start_month)
            elif args.command == 'report':
                analyzer.report(month=args.month)
            elif args.command == 'changes':
                analyzer.recent_changes(days=args.days)
            elif args.command == 'verify':
                analyzer.verify()
            elif args.command == 'compare':
                if not args.export_file:
                    print("Error: --export-file (-e) required for compare command")
                    print("Usage: python analyzer.py webui.db compare -e feedback_export.json")
                    sys.exit(1)
                analyzer.compare_export(args.export_file)
            elif args.command == 'export':
                analyzer.export_chats(args.output)
            elif args.command == 'all':
                analyzer.summary()
                analyzer.chat_volume()
                analyzer.user_stats()
                analyzer.timeline()
                analyzer.usage_stats(min_chats=min_chats, months=args.months)
                analyzer.model_usage()
                analyzer.feedback_stats(min_chats=min_chats)

            # Always report data quality issues at the end
            analyzer._report_data_quality()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
