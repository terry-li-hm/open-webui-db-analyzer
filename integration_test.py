#!/usr/bin/env python3
"""
Integration test for Open WebUI Database Analyzer

This script:
1. Spins up Open WebUI via Docker
2. Creates test users, chats, and feedback via API
3. Copies the database and runs the analyzer
4. Validates the results
5. Cleans up

Requirements:
- Docker installed and running
- Python 3.10+
- requests library (pip install requests)

Usage:
    python integration_test.py [--keep]  # --keep to not remove container after test
"""

import subprocess
import time
import sys
import os
import json
import tempfile
import shutil
import argparse
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: requests library required. Run: pip install requests")
    sys.exit(1)

# Configuration
CONTAINER_NAME = "open-webui-test"
IMAGE = "ghcr.io/open-webui/open-webui:main"
HOST_PORT = 3456  # Use non-standard port to avoid conflicts
BASE_URL = f"http://localhost:{HOST_PORT}"
STARTUP_TIMEOUT = 120  # seconds


class OpenWebUITester:
    def __init__(self):
        self.session = requests.Session()
        self.admin_token = None
        self.user_tokens = {}
        self.chat_ids = []

    def wait_for_startup(self):
        """Wait for Open WebUI to be ready."""
        print(f"Waiting for Open WebUI to start (timeout: {STARTUP_TIMEOUT}s)...")
        start = time.time()
        while time.time() - start < STARTUP_TIMEOUT:
            try:
                resp = self.session.get(f"{BASE_URL}/health", timeout=5)
                if resp.status_code == 200:
                    print("Open WebUI is ready!")
                    return True
            except requests.exceptions.ConnectionError:
                pass
            except requests.exceptions.Timeout:
                pass
            time.sleep(2)
            print(".", end="", flush=True)
        print("\nTimeout waiting for Open WebUI")
        return False

    def create_admin(self, name="Admin User", email="admin@test.com", password="testpass123"):
        """Create the first user (becomes admin)."""
        print(f"Creating admin user: {email}")
        resp = self.session.post(f"{BASE_URL}/api/v1/auths/signup", json={
            "name": name,
            "email": email,
            "password": password
        })
        if resp.status_code != 200:
            print(f"Failed to create admin: {resp.status_code} - {resp.text}")
            return False

        data = resp.json()
        self.admin_token = data.get("token")
        self.session.headers["Authorization"] = f"Bearer {self.admin_token}"
        print(f"Admin created successfully")
        return True

    def create_user(self, name, email, password="testpass123"):
        """Create a regular user."""
        print(f"Creating user: {email}")

        # Need to use admin token to create users, or signup if allowed
        resp = self.session.post(f"{BASE_URL}/api/v1/auths/signup", json={
            "name": name,
            "email": email,
            "password": password
        })

        if resp.status_code != 200:
            print(f"Failed to create user: {resp.status_code} - {resp.text}")
            return None

        data = resp.json()
        token = data.get("token")
        self.user_tokens[email] = token
        print(f"User {name} created")
        return token

    def login(self, email, password="testpass123"):
        """Login and get token."""
        resp = self.session.post(f"{BASE_URL}/api/v1/auths/signin", json={
            "email": email,
            "password": password
        })
        if resp.status_code != 200:
            print(f"Login failed: {resp.status_code}")
            return None
        return resp.json().get("token")

    def create_chat(self, token, title="Test Chat"):
        """Create a new chat."""
        headers = {"Authorization": f"Bearer {token}"}

        # Create a new chat
        resp = self.session.post(f"{BASE_URL}/api/v1/chats/new",
            headers=headers,
            json={"chat": {"title": title, "messages": []}}
        )

        if resp.status_code != 200:
            print(f"Failed to create chat: {resp.status_code} - {resp.text}")
            return None

        data = resp.json()
        chat_id = data.get("id")
        self.chat_ids.append(chat_id)
        return chat_id

    def add_message_to_chat(self, token, chat_id, role, content, model="test-model"):
        """Add a message to a chat."""
        headers = {"Authorization": f"Bearer {token}"}

        # Get current chat
        resp = self.session.get(f"{BASE_URL}/api/v1/chats/{chat_id}", headers=headers)
        if resp.status_code != 200:
            print(f"Failed to get chat: {resp.status_code}")
            return False

        chat_data = resp.json()
        chat_content = chat_data.get("chat", {})
        messages = chat_content.get("messages", [])

        # Add new message
        new_msg = {
            "id": f"msg-{len(messages)}",
            "role": role,
            "content": content,
            "timestamp": int(time.time())
        }
        if role == "assistant":
            new_msg["model"] = model

        messages.append(new_msg)
        chat_content["messages"] = messages

        # Update chat
        resp = self.session.post(f"{BASE_URL}/api/v1/chats/{chat_id}",
            headers=headers,
            json={"chat": chat_content}
        )

        return resp.status_code == 200

    def add_feedback(self, token, chat_id, message_id, rating, model_id="test-model"):
        """Add feedback (thumbs up/down) to a message."""
        headers = {"Authorization": f"Bearer {token}"}

        # The feedback API endpoint
        resp = self.session.post(f"{BASE_URL}/api/v1/feedbacks",
            headers=headers,
            json={
                "data": {
                    "rating": rating,  # 1 for thumbs up, -1 for thumbs down
                    "model_id": model_id
                },
                "meta": {
                    "chat_id": chat_id,
                    "message_id": message_id
                }
            }
        )

        if resp.status_code not in [200, 201]:
            print(f"Failed to add feedback: {resp.status_code} - {resp.text}")
            return False
        return True

    def get_models(self, token):
        """List available models."""
        headers = {"Authorization": f"Bearer {token}"}
        resp = self.session.get(f"{BASE_URL}/api/models", headers=headers)
        if resp.status_code == 200:
            return resp.json()
        return []


def run_docker_command(args, check=True):
    """Run a docker command."""
    cmd = ["docker"] + args
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"Command failed: {result.stderr}")
        return None
    return result


def start_container():
    """Start Open WebUI container."""
    print(f"\nStarting Open WebUI container: {CONTAINER_NAME}")

    # Check if container already exists
    result = run_docker_command(["ps", "-a", "--filter", f"name={CONTAINER_NAME}", "--format", "{{.Names}}"], check=False)
    if result and CONTAINER_NAME in result.stdout:
        print(f"Container {CONTAINER_NAME} already exists, removing...")
        run_docker_command(["rm", "-f", CONTAINER_NAME], check=False)

    # Start container
    result = run_docker_command([
        "run", "-d",
        "--name", CONTAINER_NAME,
        "-p", f"{HOST_PORT}:8080",
        "-e", "WEBUI_AUTH=true",
        "-e", "ENABLE_SIGNUP=true",
        IMAGE
    ])

    if result is None or result.returncode != 0:
        print("Failed to start container")
        return False

    print(f"Container started: {result.stdout.strip()[:12]}")
    return True


def stop_container():
    """Stop and remove the container."""
    print(f"\nStopping container: {CONTAINER_NAME}")
    run_docker_command(["rm", "-f", CONTAINER_NAME], check=False)


def copy_database(dest_path):
    """Copy database from container."""
    print(f"\nCopying database to {dest_path}")
    result = run_docker_command([
        "cp",
        f"{CONTAINER_NAME}:/app/backend/data/webui.db",
        dest_path
    ])
    return result is not None and result.returncode == 0


def run_analyzer(db_path):
    """Run the analyzer on the database."""
    print(f"\nRunning analyzer on {db_path}")
    script_dir = Path(__file__).parent
    analyzer_path = script_dir / "analyzer.py"

    result = subprocess.run(
        [sys.executable, str(analyzer_path), db_path, "all", "--all-users"],
        capture_output=True,
        text=True
    )

    print(result.stdout)
    if result.stderr:
        print(f"Stderr: {result.stderr}")

    return result.returncode == 0, result.stdout


def validate_results(output, expected):
    """Validate analyzer output against expected values."""
    print("\n" + "=" * 60)
    print("VALIDATION")
    print("=" * 60)

    all_passed = True
    for check_name, check_func in expected.items():
        passed = check_func(output)
        status = "✓" if passed else "✗"
        print(f"  {status} {check_name}")
        if not passed:
            all_passed = False

    return all_passed


def main():
    parser = argparse.ArgumentParser(description="Integration test for Open WebUI analyzer")
    parser.add_argument("--keep", action="store_true", help="Keep container after test")
    parser.add_argument("--skip-docker", action="store_true", help="Skip Docker setup (use existing container)")
    args = parser.parse_args()

    tester = OpenWebUITester()
    temp_dir = None
    success = False

    try:
        # Start container
        if not args.skip_docker:
            if not start_container():
                print("Failed to start container")
                return 1

        # Wait for startup
        if not tester.wait_for_startup():
            print("Open WebUI failed to start")
            return 1

        # Create test data
        print("\n" + "=" * 60)
        print("CREATING TEST DATA")
        print("=" * 60)

        # Create admin
        if not tester.create_admin("Test Admin", "admin@test.local"):
            print("Failed to create admin")
            return 1

        # Create regular users
        user1_token = tester.create_user("Alice Test", "alice@test.local")
        user2_token = tester.create_user("Bob Test", "bob@test.local")

        if not user1_token or not user2_token:
            print("Failed to create test users")
            return 1

        # Create chats for each user
        print("\nCreating chats...")

        # Alice's chats
        for i in range(3):
            chat_id = tester.create_chat(user1_token, f"Alice Chat {i+1}")
            if chat_id:
                tester.add_message_to_chat(user1_token, chat_id, "user", f"Hello from Alice, message {i+1}")
                tester.add_message_to_chat(user1_token, chat_id, "assistant", f"Hello Alice! This is response {i+1}", "gpt-4")
                # Add feedback to some messages
                if i < 2:
                    tester.add_feedback(user1_token, chat_id, "msg-1", 1, "gpt-4")  # thumbs up

        # Bob's chats
        for i in range(2):
            chat_id = tester.create_chat(user2_token, f"Bob Chat {i+1}")
            if chat_id:
                tester.add_message_to_chat(user2_token, chat_id, "user", f"Question from Bob {i+1}")
                tester.add_message_to_chat(user2_token, chat_id, "assistant", f"Answer for Bob {i+1}", "claude-3")
                # Add negative feedback
                if i == 0:
                    tester.add_feedback(user2_token, chat_id, "msg-1", -1, "claude-3")  # thumbs down

        print(f"\nCreated {len(tester.chat_ids)} chats")

        # Give the database a moment to sync
        time.sleep(2)

        # Copy database
        temp_dir = tempfile.mkdtemp(prefix="openwebui_test_")
        db_path = os.path.join(temp_dir, "webui.db")

        if not copy_database(db_path):
            print("Failed to copy database")
            return 1

        # Run analyzer
        analyzer_success, output = run_analyzer(db_path)

        if not analyzer_success:
            print("Analyzer failed")
            return 1

        # Validate results
        expected_checks = {
            "Found 3 users": lambda o: "Total Users: 3" in o,
            "Found 5 chats": lambda o: "Total Chats: 5" in o,
            "Found Alice's chats": lambda o: "Alice Test" in o,
            "Found Bob's chats": lambda o: "Bob Test" in o,
            "Feedback section present": lambda o: "FEEDBACK ANALYSIS" in o,
            "No critical parse errors": lambda o: "DATA QUALITY WARNINGS" not in o or "JSON Parse Errors" not in o,
        }

        success = validate_results(output, expected_checks)

        if success:
            print("\n✓ All validation checks passed!")
        else:
            print("\n✗ Some validation checks failed")

        return 0 if success else 1

    except KeyboardInterrupt:
        print("\nInterrupted")
        return 1
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        # Cleanup
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

        if not args.keep and not args.skip_docker:
            stop_container()


if __name__ == "__main__":
    sys.exit(main())
