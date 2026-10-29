#!/usr/bin/env python3
"""
setup_project.py — LucidLink project onboarding homework.

WHAT THIS SCRIPT AUTOMATES
--------------------------
1. Creates the requested filespace through the LucidLink REST API, or reuses it
   if it already exists.
2. Invites the configured team member, or reuses the member if already present.
3. Creates the configured group, or reuses it if already present.
4. Adds the member to the group if the relationship is not already present.
5. Grants the group read access to the filespace root (/) if that permission is
   not already present.
6. Creates /Raw_Footage through the REST API if it does not already exist.
7. Copies a local sample asset through the mounted LucidLink filesystem.
8. Runs LucidLink's sync command, polls "Remaining upload" until it reaches
   zero, and exits only the daemon instance for the target filespace.

IMPORTANT OPERATING NOTES
-------------------------
- This uses Python's standard library only; no pip installation is required.
- The REST API base URL defaults to the local API container used in the homework:
  http://localhost:3003/api/v1
- A service-account token is read from .env (LL_TOKEN). Never commit .env.
- The API can create a filespace, but the LucidLink client still has to link and
  mount that filespace before local file ingest can happen. If the filespace is
  not currently mounted/running, the script stops with instructions rather than
  guessing CLI link arguments or shutting down the wrong daemon.
- The daemon is identified by matching the filespace name in `lucid list`, and
  all cache/sync/exit commands include that instance ID. This avoids affecting
  the unrelated Recruiting daemon.
- This script intentionally does not delete cloud resources on failure.
"""

from __future__ import annotations

# Future annotations let type hints refer to types without evaluating every
# annotation immediately. This is useful for modern Python type hints.

# json: encode request bodies and inspect API responses.
import json
# os: read environment variables, including the private service-account token.
import os
# re: find the Remaining upload value in the CLI output.
import re
# shutil: copy the sample asset into the mounted filespace.
import shutil
# subprocess: call the installed LucidLink CLI without shell-string execution.
import subprocess
# sys: send errors to stderr and return a process exit code.
import sys
# time: create a sample timestamp, pause between polls, and enforce a timeout.
import time
# Path makes filesystem paths safer and clearer than hand-built strings.
from pathlib import Path
# Any accommodates response objects whose exact API schema can vary.
from typing import Any
# HTTPError handles HTTP status failures; URLError handles connection failures.
from urllib.error import HTTPError, URLError
# urlencode safely formats query-string parameters such as ?email=... .
from urllib.parse import urlencode
# urllib is part of Python's standard library, so no requests package is needed.
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Keep local configuration in .env. Values shown here are safe defaults for
# the homework environment; do not put the service-account token in this file.
# Resolve paths relative to this script so it works when launched from another
# working directory. The real .env file stays beside the script and is ignored by Git.
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"


def load_env_file(path: Path) -> None:
    """
    Read simple KEY=VALUE lines from .env into os.environ.

    This deliberately implements only the small subset needed here. It skips
    blank lines and comments, supports optional single/double quotes around a
    value, and does not execute shell syntax.
    """
    # Missing .env is not fatal by itself: an environment variable may have been
    # exported in the shell instead. main() checks that a token was actually found.
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        # Shell-exported values take precedence, making temporary overrides easy.
        if key and key not in os.environ:
            os.environ[key] = value


# Load local settings before reading the configuration constants below.
load_env_file(ENV_FILE)

# Defaults match the local homework environment; .env can override each value.
API_BASE_URL = os.getenv("LL_API_BASE_URL", "http://localhost:3003/api/v1").rstrip("/")
TOKEN = os.getenv("LL_TOKEN", "").strip()
FILESPACE_NAME = os.getenv("LL_FILESPACE_NAME", "project-alpha-2026").strip()
AWS_REGION = os.getenv("LL_REGION", "us-west-1").strip()
MEMBER_EMAIL = os.getenv("LL_MEMBER_EMAIL", "jerichosense@gmail.com").strip()
GROUP_NAME = os.getenv("LL_GROUP_NAME", "Video_Editors").strip()
MOUNT_PATH = Path(
    os.getenv(
        "LL_MOUNT_PATH",
        "/Volumes/jozefs-space/project-alpha-2026",
    )
).expanduser()
SAMPLE_SOURCE = Path(
    os.getenv(
        "LL_SAMPLE_SOURCE",
        str(SCRIPT_DIR / "sample-assets" / "sample-footage.txt"),
    )
).expanduser()
UPLOAD_POLL_SECONDS = int(os.getenv("LL_UPLOAD_POLL_SECONDS", "5"))
UPLOAD_TIMEOUT_SECONDS = int(os.getenv("LL_UPLOAD_TIMEOUT_SECONDS", "600"))


# ---------------------------------------------------------------------------
# REST API helpers
# These functions handle HTTP details once so the workflow functions can focus
# on the resource being managed. All authenticated calls share api_request().
# ---------------------------------------------------------------------------
class ApiError(RuntimeError):
    """Raised when the LucidLink REST API returns an unsuccessful response."""


def api_request(
    method: str,
    endpoint: str,
    *,
    payload: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    expected_statuses: tuple[int, ...] = (200, 201),
) -> dict[str, Any] | None:
    """
    Send one authenticated REST request.

    `endpoint` is a path below /api/v1, such as "/filespaces".
    The token is added only as an Authorization header and is never printed.
    A caller may provide extra accepted status codes for operations where 404
    means "not found yet" and should be handled by the caller.
    """
    # Build the full URL from the configured API root and a resource path.
    url = f"{API_BASE_URL}{endpoint}"
    if query:
        url = f"{url}?{urlencode(query)}"

    # GET requests usually have no body. POST/PATCH payloads are serialized as JSON.
    body_bytes = None
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
    }
    if payload is not None:
        body_bytes = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    # Pass the HTTP method explicitly; do not invoke a shell to run this request.
    request = Request(url, data=body_bytes, headers=headers, method=method.upper())

    # Bound network waits so a stalled API does not hang the setup indefinitely.
    try:
        with urlopen(request, timeout=30) as response:
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
        if status not in expected_statuses:
            # API error bodies are useful for debugging, but avoid printing
            # request headers or the service-account token.
            raise ApiError(f"{method} {endpoint} returned HTTP {status}: {raw}") from None
    except URLError as exc:
        raise ApiError(
            f"Could not reach {url}. Is the LucidLink API container running? ({exc})"
        ) from None

    # Treat only explicitly accepted status codes as success; do not silently
    # continue after authentication, validation, or server errors.
    if status not in expected_statuses:
        raise ApiError(f"{method} {endpoint} returned HTTP {status}: {raw}")

    if not raw.strip():
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(f"{method} {endpoint} returned non-JSON content.") from None

    # Most endpoints wrap the result in {"data": ...}; preserve the whole
    # response so callers can inspect other fields if an endpoint returns them.
    return parsed


def data_of(response: dict[str, Any] | None) -> Any:
    """Return the API's standard `data` value, if present."""
    if not response:
        return None
    return response.get("data", response)


def nested_email(item: dict[str, Any]) -> str:
    """Member responses nest the email inside `user`; tolerate either shape."""
    user = item.get("user")
    if isinstance(user, dict):
        return str(user.get("email", "")).strip().lower()
    return str(item.get("email", "")).strip().lower()


def get_filespace() -> dict[str, Any]:
    """
    Find the configured filespace by name, creating it if it is absent.

    The create body follows the example in the supplied LucidLink API reference.
    Reusing a matching name prevents duplicate filespaces on repeated runs.
    """
    # Read before creating: reruns should reuse the named filespace, not duplicate it.
    result = data_of(api_request("GET", "/filespaces"))
    filespaces = result if isinstance(result, list) else []
    for filespace in filespaces:
        if filespace.get("name") == FILESPACE_NAME:
            print(f"[filespace] Reusing existing filespace: {FILESPACE_NAME}")
            return filespace

    print(f"[filespace] Creating filespace: {FILESPACE_NAME}")
    response = data_of(
        api_request(
            "POST",
            "/filespaces",
            payload={
                "name": FILESPACE_NAME,
                "region": AWS_REGION,
                "storageOwner": "lucidlink",
                "storageProvider": "AWS",
            },
        )
    )
    if not isinstance(response, dict) or not response.get("id"):
        raise ApiError("Filespace create response did not contain a filespace ID.")
    print(f"[filespace] Created filespace ID: {response['id']}")
    return response


def get_or_invite_member() -> dict[str, Any]:
    """Return an existing workspace member or invite the configured email."""
    # Filter by email first so a repeat run does not invite the same person again.
    response = data_of(
        api_request("GET", "/members", query={"email": MEMBER_EMAIL})
    )
    members = response if isinstance(response, list) else []

    for member in members:
        if nested_email(member) == MEMBER_EMAIL.lower():
            print(
                f"[member] Reusing {MEMBER_EMAIL} "
                f"(status: {member.get('status', 'unknown')})"
            )
            return member

    print(f"[member] Inviting {MEMBER_EMAIL}")
    member = data_of(
        api_request("POST", "/members", payload={"email": MEMBER_EMAIL})
    )
    if not isinstance(member, dict) or not member.get("id"):
        raise ApiError("Member invite response did not contain a member ID.")

    # Do not print the entire response: the API may include a private invitation
    # link/secret in the response body.
    print(
        f"[member] Invitation created. Member ID: {member['id']}; "
        f"status: {member.get('status', 'unknown')}"
    )
    return member


def get_or_create_group() -> dict[str, Any]:
    """Return the named group, or create it if it is not already present."""
    # Search for the named group first to keep the setup safe to rerun.
    response = data_of(
        api_request("GET", "/groups", query={"name": GROUP_NAME})
    )
    groups = response if isinstance(response, list) else []

    for group in groups:
        if group.get("name") == GROUP_NAME:
            print(f"[group] Reusing existing group: {GROUP_NAME}")
            return group

    print(f"[group] Creating group: {GROUP_NAME}")
    group = data_of(
        api_request("POST", "/groups", payload={"name": GROUP_NAME})
    )
    if not isinstance(group, dict) or not group.get("id"):
        raise ApiError("Group create response did not contain a group ID.")
    print(f"[group] Created group ID: {group['id']}")
    return group


def add_member_to_group(group_id: str, member_id: str) -> None:
    """
    Add the member to the group.

    First inspect the group-member listing. The response's precise nested shape
    may vary by API version, so we look for the member ID/email anywhere in the
    returned JSON before deciding whether the relationship already exists.
    """
    # Check current membership before PUT; avoid unnecessary duplicate operations.
    response = api_request("GET", f"/groups/{group_id}/members")
    serialized = json.dumps(response or {})
    if member_id in serialized or MEMBER_EMAIL.lower() in serialized.lower():
        print(f"[membership] {MEMBER_EMAIL} is already in {GROUP_NAME}")
        return

    print(f"[membership] Adding {MEMBER_EMAIL} to {GROUP_NAME}")
    # This endpoint uses path parameters only; the API documentation defines
    # no request body for this PUT.
    api_request("PUT", f"/groups/{group_id}/members/{member_id}")
    print("[membership] Member added to group.")


def ensure_root_read_permission(filespace_id: str, group_id: str) -> None:
    """
    Ensure the group has read permission on the filespace root (/).

    The API supports filtering permissions by principalId. We still check both
    path and permission values before deciding that the desired grant exists.
    """
    # Restrict the listing to this group, then verify the root path and read grant.
    response = data_of(
        api_request(
            "GET",
            f"/filespaces/{filespace_id}/permissions",
            query={"principalId": group_id},
        )
    )
    permissions = response if isinstance(response, list) else []

    for permission in permissions:
        if (
            permission.get("principalId") == group_id
            and permission.get("path") == "/"
            and "read" in permission.get("permissions", [])
        ):
            print(f"[permission] {GROUP_NAME} already has read access to /")
            return

    print(f"[permission] Granting {GROUP_NAME} read access to /")
    api_request(
        "POST",
        f"/filespaces/{filespace_id}/permissions",
        payload={
            "path": "/",
            "permissions": ["read"],
            "principalId": group_id,
        },
    )
    print("[permission] Root read permission created.")


def resolve_entry(filespace_id: str, path: str) -> dict[str, Any] | None:
    """
    Resolve a filespace path to its entry metadata.

    A 404 is returned as None so the caller can create the missing directory.
    Other API errors still stop the script.
    """
    # Resolve by path to obtain the server-side entry ID needed by create-entry.
    endpoint = f"/filespaces/{filespace_id}/entries/resolve"
    try:
        response = api_request(
            "GET",
            endpoint,
            query={"path": path},
            expected_statuses=(200, 404),
        )
    except ApiError:
        raise

    # api_request returns a parsed body for both success and expected HTTP 404.
    # On 404 the body is an error object, not entry data.
    if not response or "error" in response:
        return None
    entry = data_of(response)
    return entry if isinstance(entry, dict) and entry.get("id") else None


def ensure_raw_footage_directory(filespace_id: str) -> None:
    """Create /Raw_Footage through REST if it does not already exist."""
    # First check whether the target path already exists; do not create duplicates.
    existing = resolve_entry(filespace_id, "/Raw_Footage")
    if existing:
        if existing.get("type") != "dir":
            raise ApiError("/Raw_Footage exists but is not a directory.")
        print(f"[directory] Reusing /Raw_Footage (entry ID {existing['id']})")
        return

    root = resolve_entry(filespace_id, "/")
    if not root:
        raise ApiError("Could not resolve the filespace root path '/'.")
    root_id = root["id"]

    print("[directory] Creating /Raw_Footage through the REST API")
    entry = data_of(
        api_request(
            "POST",
            f"/filespaces/{filespace_id}/entries",
            payload={"parentId": root_id, "name": "Raw_Footage", "type": "dir"},
        )
    )
    if not isinstance(entry, dict) or not entry.get("id"):
        raise ApiError("Directory create response did not contain an entry ID.")
    print(f"[directory] Created /Raw_Footage (entry ID {entry['id']})")


# ---------------------------------------------------------------------------
# Local filesystem and daemon helpers
# These operations run on the Mac, not inside the API container. The API manages
# cloud metadata; the local CLI and mounted volume handle file bytes and sync.
# ---------------------------------------------------------------------------
def ensure_sample_asset() -> Path:
    """
    Ensure a small, non-sensitive sample asset exists locally.

    For a real media ingest, set LL_SAMPLE_SOURCE to the path of a sample clip.
    The default text file keeps this homework test lightweight and reproducible.
    """
    # Ensure the local sample-assets directory exists before writing a fallback file.
    SAMPLE_SOURCE.parent.mkdir(parents=True, exist_ok=True)
    if not SAMPLE_SOURCE.exists():
        SAMPLE_SOURCE.write_text(
            "LucidLink homework sample asset\n"
            f"Created: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n",
            encoding="utf-8",
        )
        print(f"[sample] Created local sample asset: {SAMPLE_SOURCE}")
    return SAMPLE_SOURCE


def find_target_instance_id() -> str:
    """
    Find the running LucidLink daemon for this filespace from `lucid list`.

    The CLI lists instance ID and filespace name. We only match lines containing
    the configured filespace name and take the first column as the instance ID.
    If the target daemon is not running, we stop rather than accidentally using
    the Recruiting instance or another daemon.
    """
    # `lucid list` is the source of truth for currently running client instances.
    # Use an argument list (not shell=True) and a timeout for safer process execution.
    try:
        result = subprocess.run(
            ["lucid", "list"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not run `lucid list`: {exc}") from None

    # Match the configured filespace name, not merely the first listed daemon.
    matches: list[str] = []
    for line in result.stdout.splitlines():
        if FILESPACE_NAME in line:
            columns = line.split()
            if columns and columns[0].isdigit():
                matches.append(columns[0])

    if not matches:
        raise RuntimeError(
            f"No running LucidLink instance for '{FILESPACE_NAME}' was found.\n"
            "The REST setup may be complete, but local ingest requires this "
            "filespace to be linked, mounted, and running. Start/link it using "
            "the LucidLink client, confirm it appears in `lucid list`, then "
            "rerun this script. The script will reuse the API resources."
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"More than one daemon matched '{FILESPACE_NAME}': {matches}. "
            "Refusing to guess which instance to operate on."
        )
    return matches[0]


def run_lucid(instance_id: str, command: str) -> str:
    """Run one CLI command against only the explicitly selected instance."""
    # Every client command is scoped to the selected instance ID. This is important
    # when other LucidLink filespaces (for example, Recruiting) are also running.
    try:
        result = subprocess.run(
            ["lucid", "--instance", instance_id, command],
            check=True,
            capture_output=True,
            text=True,
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(
            f"`lucid --instance {instance_id} {command}` failed: {details}"
        ) from None
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not complete LucidLink command '{command}': {exc}") from None

    output = (result.stdout or "").strip()
    if output:
        print(output)
    return output


def parse_remaining_upload_bytes(cache_output: str) -> int:
    """
    Parse the CLI's 'Remaining upload' line into bytes.

    The known CLI output looks like 'Remaining upload: 0B' or '0 B'. This parser
    accepts common byte units and raises an error if the line is absent, rather
    than assuming that a missing value means syncing is complete.
    """
    # Require an explicit, parseable upload counter; missing output is not success.
    match = re.search(
        r"Remaining upload:\s*([0-9]+(?:\.[0-9]+)?)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)\b",
        cache_output,
        flags=re.IGNORECASE,
    )
    if not match:
        raise RuntimeError(
            "Could not find a recognizable 'Remaining upload' value in "
            "`lucid cache` output; refusing to shut down the daemon."
        )

    value = float(match.group(1))
    unit = match.group(2).lower()
    powers = {
        "b": 0,
        "kib": 1, "mib": 2, "gib": 3, "tib": 4,
        "kb": 1, "mb": 2, "gb": 3, "tb": 4,
    }
    # LucidLink reports binary units (KiB/MiB/GiB); decimal units are handled
    # conservatively here for compatibility with possible CLI versions.
    base = 1024 if "i" in unit else 1000
    return int(value * (base ** powers[unit]))


def wait_for_uploads_to_finish(instance_id: str) -> None:
    """
    Synchronize, then poll the cache until the reported upload backlog is zero.

    A timeout intentionally leaves the daemon running. Shutting it down while
    uploads remain would violate the homework's safe-exit requirement.
    """
    print("[sync] Requesting LucidLink synchronization")
    run_lucid(instance_id, "sync")

    # A monotonic clock is appropriate for elapsed-time limits because it is not
    # affected by system clock changes.
    deadline = time.monotonic() + UPLOAD_TIMEOUT_SECONDS
    while True:
        cache_output = run_lucid(instance_id, "cache")
        remaining = parse_remaining_upload_bytes(cache_output)
        if remaining == 0:
            print("[sync] Remaining upload is 0 B.")
            return

        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Timed out with {remaining} bytes still reported for upload. "
                "The daemon has NOT been shut down."
            )

        print(
            f"[sync] {remaining} bytes remain; checking again in "
            f"{UPLOAD_POLL_SECONDS} seconds..."
        )
        time.sleep(UPLOAD_POLL_SECONDS)


def copy_sample_to_mount() -> Path:
    """
    Copy the local sample asset into the mounted /Raw_Footage folder.

    This is a real filesystem copy through the LucidLink mount; it is not an API
    upload. The mounted destination must be backed by the target running daemon.
    """
    # The API creates the directory entry; the mounted client exposes it locally.
    destination_dir = MOUNT_PATH / "Raw_Footage"
    if not MOUNT_PATH.is_dir():
        raise RuntimeError(
            f"Mount path is not accessible: {MOUNT_PATH}\n"
            "Link and mount the target filespace, then rerun the script."
        )
    if not destination_dir.is_dir():
        raise RuntimeError(
            f"Expected directory is not accessible: {destination_dir}. "
            "Confirm the REST-created directory has appeared in the mount."
        )

    # Copy only after confirming the target directory is accessible through the mount.
    source = ensure_sample_asset()
    destination = destination_dir / source.name
    shutil.copy2(source, destination)
    print(f"[ingest] Copied {source} -> {destination} ({destination.stat().st_size} bytes)")
    return destination


def verify_instance_exited(instance_id: str) -> None:
    """Check `lucid list` and fail if the target instance is still listed."""
    result = subprocess.run(
        ["lucid", "list"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    for line in result.stdout.splitlines():
        columns = line.split()
        if columns and columns[0] == instance_id:
            raise RuntimeError(
                f"Instance {instance_id} still appears in `lucid list`; "
                "please inspect the daemon state manually."
            )
    print(f"[shutdown] Verified instance {instance_id} is no longer listed.")
    print("[shutdown] Other LucidLink instances were not targeted.")


# ---------------------------------------------------------------------------
# Main workflow
# Keep the steps in this order: provision resources, ingest data, sync, then exit.
# ---------------------------------------------------------------------------
def main() -> int:
    """Run the onboarding workflow in order, stopping safely on any error."""
    # Fail before making any API changes if credentials are missing.
    if not TOKEN:
        print(
            "ERROR: LL_TOKEN is not set. Copy .env.example to .env and add "
            "your service-account token to LL_TOKEN. Do not paste the token "
            "into chat or commit .env.",
            file=sys.stderr,
        )
        return 2

    if not FILESPACE_NAME or not MEMBER_EMAIL or not GROUP_NAME:
        print("ERROR: Filespace, member email, and group name must be non-empty.", file=sys.stderr)
        return 2

    try:
        print("=== LucidLink project setup ===")
        print(f"API: {API_BASE_URL}")
        print(f"Filespace: {FILESPACE_NAME}")
        print(f"Member: {MEMBER_EMAIL}")
        print(f"Group: {GROUP_NAME}")

        # Phase 1: provision cloud-side resources and access controls via REST.
        # Each helper checks for an existing resource before attempting creation.
        filespace = get_filespace()
        filespace_id = str(filespace["id"])

        member = get_or_invite_member()
        member_id = str(member["id"])

        group = get_or_create_group()
        group_id = str(group["id"])

        add_member_to_group(group_id, member_id)
        ensure_root_read_permission(filespace_id, group_id)
        ensure_raw_footage_directory(filespace_id)

        # Phase 2: local ingest. The REST API creates metadata, but the actual bytes
        # must be copied through the mounted filesystem by the local client.
        # Verify the correct daemon is running before copying anything.
        instance_id = find_target_instance_id()
        copy_sample_to_mount()

        # Phase 3: sync and verify. A zero upload backlog is a prerequisite for exit.
        # If the counter is missing or nonzero through timeout, the script keeps the
        # daemon running instead of risking an incomplete upload.
        wait_for_uploads_to_finish(instance_id)

        print(f"[shutdown] Exiting only target daemon instance {instance_id}")
        run_lucid(instance_id, "exit")
        verify_instance_exited(instance_id)

        print("\nSUCCESS: Project setup and safe shutdown completed.")
        return 0

    # Centralized failure handling gives a useful error while preserving resources.
    # No automatic deletes/rollback are attempted because they could destroy valid work.
    except (ApiError, RuntimeError, OSError, ValueError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print(
            "The script stopped without deleting resources. If the REST setup "
            "already succeeded, rerunning it will reuse matching resources.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
