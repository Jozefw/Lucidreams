
#!/usr/bin/env python3
"""
setup_project.py — LucidLink project onboarding homework.

WHAT THIS SCRIPT AUTOMATES
--------------------------
1. Creates the requested filespace or reuses it if it already exists.
2. Invites the configured team member or reuses the existing member.
3. Creates the configured group or reuses it if it already exists.
4. Adds the member to the group if not already present.
5. Grants the group read access to the filespace root (/).
6. Creates /Raw_Footage through the REST API if it does not exist.
7. Copies a local sample asset through the mounted LucidLink filesystem.
8. Runs LucidLink sync and waits until Remaining upload reaches zero.
9. Exits only the daemon instance belonging to the target filespace.

IMPORTANT OPERATING NOTES
-------------------------
- Uses Python's standard library only. No pip installation is required.
- The REST API defaults to http://localhost:3003/api/v1.
- The service-account token is read from .env (LL_TOKEN).
- Never commit .env or share the token.
- The filespace must be linked and mounted before local file copying.
- The target daemon is identified by matching the filespace name in
  `lucid list`. All CLI commands use that specific instance ID.
- The script does not delete cloud resources if an operation fails.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# IMPORTS
# ---------------------------------------------------------------------------

import json
import os
import re
import shutil
import subprocess
import sys
import time

from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

# Resolve paths relative to this script so it works regardless of the
# directory from which the script is launched.
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"


def load_env_file(path: Path) -> None:
    """
    Read simple KEY=VALUE entries from a .env file.

    Supports:
    - Blank lines
    - Comment lines beginning with #
    - Optional single or double quotes around values

    Existing shell environment variables take precedence.
    This function does not execute shell commands.
    """
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        # Remove matching quotes around a value.
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]

        # Do not overwrite values already exported in the shell.
        if key and key not in os.environ:
            os.environ[key] = value


# Load .env before reading configuration values.
load_env_file(ENV_FILE)

# API connection settings.
API_BASE_URL = os.getenv(
    "LL_API_BASE_URL",
    "http://localhost:3003/api/v1",
).rstrip("/")

TOKEN = os.getenv("LL_TOKEN", "").strip()

# Homework project settings.
FILESPACE_NAME = os.getenv(
    "LL_FILESPACE_NAME",
    "project-alpha-2026",
).strip()

AWS_REGION = os.getenv("LL_REGION", "us-west-1").strip()

MEMBER_EMAIL = os.getenv(
    "LL_MEMBER_EMAIL",
    "jerichosense@gmail.com",
).strip()

GROUP_NAME = os.getenv(
    "LL_GROUP_NAME",
    "Video_Editors",
).strip()

# Local LucidLink mount location.
MOUNT_PATH = Path(
    os.getenv(
        "LL_MOUNT_PATH",
        "/Volumes/jozefs-space/project-alpha-2026",
    )
).expanduser()

# Local sample asset used for the ingest operation.
SAMPLE_SOURCE = Path(
    os.getenv(
        "LL_SAMPLE_SOURCE",
        str(SCRIPT_DIR / "sample-assets" / "sample-footage.txt"),
    )
).expanduser()

# Upload polling settings.
UPLOAD_POLL_SECONDS = int(
    os.getenv("LL_UPLOAD_POLL_SECONDS", "5")
)

UPLOAD_TIMEOUT_SECONDS = int(
    os.getenv("LL_UPLOAD_TIMEOUT_SECONDS", "600")
)


# ---------------------------------------------------------------------------
# REST API HELPERS
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
    Send an authenticated request to the LucidLink REST API.

    Args:
        method: HTTP method such as GET, POST, PUT, or PATCH.
        endpoint: API path below /api/v1.
        payload: Optional JSON request body.
        query: Optional query-string parameters.
        expected_statuses: HTTP statuses accepted by this operation.

    Returns:
        Parsed JSON response, or None for an empty response.

    The service-account token is sent in the Authorization header.
    It is never printed.
    """

    # Construct the full API URL.
    url = f"{API_BASE_URL}{endpoint}"

    # Add query parameters when supplied.
    if query:
        url = f"{url}?{urlencode(query)}"

    # Prepare request headers.
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
    }

    # Serialize an optional request body.
    body_bytes = None

    if payload is not None:
        body_bytes = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    # Build the HTTP request without using shell execution.
    request = Request(
        url,
        data=body_bytes,
        headers=headers,
        method=method.upper(),
    )

    # Execute the request with a timeout.
    try:
        with urlopen(request, timeout=30) as response:
            status = response.status
            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

    except HTTPError as exc:
        status = exc.code
        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        if status not in expected_statuses:
            raise ApiError(
                f"{method} {endpoint} returned HTTP {status}: {raw}"
            ) from None

    except URLError as exc:
        raise ApiError(
            f"Could not reach {url}. "
            f"Is the LucidLink API container running? ({exc})"
        ) from None

    # Confirm that the returned status is allowed.
    if status not in expected_statuses:
        raise ApiError(
            f"{method} {endpoint} returned HTTP {status}: {raw}"
        )

    # Some successful API operations return an empty response.
    if not raw.strip():
        return None

    # Parse the JSON response.
    try:
        parsed = json.loads(raw)

    except json.JSONDecodeError:
        raise ApiError(
            f"{method} {endpoint} returned non-JSON content."
        ) from None

    return parsed


def data_of(response: dict[str, Any] | None) -> Any:
    """
    Return the API's standard data value when present.

    Some responses use a wrapper such as {"data": ...}.
    Others return the resource directly.
    """
    if not response:
        return None

    return response.get("data", response)


def nested_email(item: dict[str, Any]) -> str:
    """
    Extract an email address from a member response.

    The API may nest the email under a user object or return it
    directly on the member object.
    """
    user = item.get("user")

    if isinstance(user, dict):
        return str(
            user.get("email", "")
        ).strip().lower()

    return str(
        item.get("email", "")
    ).strip().lower()


# ---------------------------------------------------------------------------
# FILESPACE PROVISIONING
# ---------------------------------------------------------------------------

def get_filespace() -> dict[str, Any]:
    """
    Find the configured filespace.

    If it already exists, reuse it.
    Otherwise, create it through the REST API.
    """

    result = data_of(
        api_request("GET", "/filespaces")
    )

    filespaces = result if isinstance(result, list) else []

    # Reuse an existing filespace with the configured name.
    for filespace in filespaces:
        if filespace.get("name") == FILESPACE_NAME:
            print(
                f"[filespace] Reusing existing filespace: "
                f"{FILESPACE_NAME}"
            )
            return filespace

    # Create the filespace when no matching name exists.
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
        raise ApiError(
            "Filespace create response did not contain a filespace ID."
        )

    print(
        f"[filespace] Created filespace ID: {response['id']}"
    )

    return response


# ---------------------------------------------------------------------------
# MEMBER AND GROUP MANAGEMENT
# ---------------------------------------------------------------------------

def get_or_invite_member() -> dict[str, Any]:
    """
    Find the configured workspace member.

    If the member is not present, send an invitation.
    """

    response = data_of(
        api_request(
            "GET",
            "/members",
            query={"email": MEMBER_EMAIL},
        )
    )

    members = response if isinstance(response, list) else []

    # Reuse the member if the email already exists.
    for member in members:
        if nested_email(member) == MEMBER_EMAIL.lower():
            print(
                f"[member] Reusing {MEMBER_EMAIL} "
                f"(status: {member.get('status', 'unknown')})"
            )
            return member

    # Invite the member if no matching account exists.
    print(f"[member] Inviting {MEMBER_EMAIL}")

    member = data_of(
        api_request(
            "POST",
            "/members",
            payload={"email": MEMBER_EMAIL},
        )
    )

    if not isinstance(member, dict) or not member.get("id"):
        raise ApiError(
            "Member invite response did not contain a member ID."
        )

    # Do not print the entire response because it may contain private
    # invitation information.
    print(
        f"[member] Invitation created. "
        f"Member ID: {member['id']}; "
        f"status: {member.get('status', 'unknown')}"
    )

    return member


def get_or_create_group() -> dict[str, Any]:
    """
    Find the configured group.

    If it does not exist, create it.
    """

    response = data_of(
        api_request(
            "GET",
            "/groups",
            query={"name": GROUP_NAME},
        )
    )

    groups = response if isinstance(response, list) else []

    # Reuse an existing group with the configured name.
    for group in groups:
        if group.get("name") == GROUP_NAME:
            print(
                f"[group] Reusing existing group: {GROUP_NAME}"
            )
            return group

    # Create the group if it does not already exist.
    print(f"[group] Creating group: {GROUP_NAME}")

    group = data_of(
        api_request(
            "POST",
            "/groups",
            payload={"name": GROUP_NAME},
        )
    )

    if not isinstance(group, dict) or not group.get("id"):
        raise ApiError(
            "Group create response did not contain a group ID."
        )

    print(f"[group] Created group ID: {group['id']}")

    return group


def add_member_to_group(
    group_id: str,
    member_id: str,
) -> None:
    """
    Add the configured member to the group.

    First inspect the group-member listing to avoid repeating
    an operation that has already been completed.
    """

    response = api_request(
        "GET",
        f"/groups/{group_id}/members",
    )

    # Serialize the response so the member ID or email can be searched
    # even if the API returns a nested response structure.
    serialized = json.dumps(response or {})

    if (
        member_id in serialized
        or MEMBER_EMAIL.lower() in serialized.lower()
    ):
        print(
            f"[membership] {MEMBER_EMAIL} "
            f"is already in {GROUP_NAME}"
        )
        return

    # Add the member when the relationship is not present.
    print(
        f"[membership] Adding {MEMBER_EMAIL} to {GROUP_NAME}"
    )

    api_request(
        "PUT",
        f"/groups/{group_id}/members/{member_id}",
    )

    print("[membership] Member added to group.")


# ---------------------------------------------------------------------------
# ROOT READ PERMISSION
# ---------------------------------------------------------------------------

def ensure_root_read_permission(
    filespace_id: str,
    group_id: str,
) -> None:
    """
    Ensure the group has read permission on the filespace root (/).

    Reuse an existing matching grant instead of creating a duplicate.

    The API may return permissions as either:
        {"permissions": [...]}

    or as a bare list:
        [...]
    """

    response = data_of(
        api_request(
            "GET",
            f"/filespaces/{filespace_id}/permissions",
            query={"principalId": group_id},
        )
    )

    # The API returns {"permissions": [...]} rather than a bare list.
    # Also support a bare list in case the response format differs.
    if isinstance(response, dict):
        permissions = response.get("permissions", [])

    elif isinstance(response, list):
        permissions = response

    else:
        raise ApiError(
            "Permission lookup returned an unexpected response structure."
        )

    # Do not continue if the permissions value is malformed.
    if not isinstance(permissions, list):
        raise ApiError(
            "Permission lookup did not contain a valid permissions list."
        )

    # Check each returned permission for an exact match.
    for permission in permissions:
        if not isinstance(permission, dict):
            continue

        if (
            permission.get("principalId") == group_id
            and permission.get("principalType") == "group"
            and permission.get("path") == "/"
            and "read" in permission.get("permissions", [])
        ):
            print(
                f"[permission] {GROUP_NAME} "
                f"already has read access to /"
            )
            return

    # No matching root read grant was found. Create it.
    print(
        f"[permission] Granting {GROUP_NAME} read access to /"
    )

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


# ---------------------------------------------------------------------------
# FILESPACE DIRECTORY MANAGEMENT
# ---------------------------------------------------------------------------

def resolve_entry(
    filespace_id: str,
    path: str,
) -> dict[str, Any] | None:
    """
    Resolve a filespace path to its entry metadata.

    A 404 means the path was not found, so return None.
    Other API errors stop the script.
    """

    endpoint = (
        f"/filespaces/{filespace_id}/entries/resolve"
    )

    response = api_request(
        "GET",
        endpoint,
        query={"path": path},
        expected_statuses=(200, 404),
    )

    # A 404 response is an error body, not entry metadata.
    if not response or "error" in response:
        return None

    entry = data_of(response)

    if isinstance(entry, dict) and entry.get("id"):
        return entry

    return None


def ensure_raw_footage_directory(
    filespace_id: str,
) -> None:
    """
    Ensure /Raw_Footage exists in the filespace.

    The directory is created through the REST API.
    """

    # Check whether the directory already exists.
    existing = resolve_entry(
        filespace_id,
        "/Raw_Footage",
    )

    if existing:
        # Do not accept a file where a directory is expected.
        if existing.get("type") != "dir":
            raise ApiError(
                "/Raw_Footage exists but is not a directory."
            )

        print(
            f"[directory] Reusing /Raw_Footage "
            f"(entry ID {existing['id']})"
        )
        return

    # Resolve the root directory so its entry ID can be used as parentId.
    root = resolve_entry(filespace_id, "/")

    if not root:
        raise ApiError(
            "Could not resolve the filespace root path '/'."
        )

    root_id = root["id"]

    # Create the Raw_Footage directory under the root.
    print(
        "[directory] Creating /Raw_Footage through the REST API"
    )

    entry = data_of(
        api_request(
            "POST",
            f"/filespaces/{filespace_id}/entries",
            payload={
                "parentId": root_id,
                "name": "Raw_Footage",
                "type": "dir",
            },
        )
    )

    if not isinstance(entry, dict) or not entry.get("id"):
        raise ApiError(
            "Directory create response did not contain an entry ID."
        )

    print(
        f"[directory] Created /Raw_Footage "
        f"(entry ID {entry['id']})"
    )


# ---------------------------------------------------------------------------
# LOCAL SAMPLE FILE
# ---------------------------------------------------------------------------

def ensure_sample_asset() -> Path:
    """
    Ensure a small sample asset exists locally.

    If the configured sample file is missing, create a simple text file.
    A real media file can be supplied through LL_SAMPLE_SOURCE.
    """

    # Make sure the local sample-assets directory exists.
    SAMPLE_SOURCE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Create a lightweight sample file if one is not already present.
    if not SAMPLE_SOURCE.exists():
        SAMPLE_SOURCE.write_text(
            "LucidLink homework sample asset\n"
            f"Created: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n",
            encoding="utf-8",
        )

        print(
            f"[sample] Created local sample asset: {SAMPLE_SOURCE}"
        )

    return SAMPLE_SOURCE


# ---------------------------------------------------------------------------
# LUCIDLINK DAEMON IDENTIFICATION
# ---------------------------------------------------------------------------

def find_target_instance_id() -> str:
    """
    Find the running LucidLink daemon for the configured filespace.

    The script matches the filespace name in `lucid list`.
    It does not assume that the first listed daemon is the correct one.

    If the target cannot be identified unambiguously, stop.
    """

    try:
        result = subprocess.run(
            ["lucid", "list"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )

    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise RuntimeError(
            f"Could not run `lucid list`: {exc}"
        ) from None

    # Collect instance IDs from lines containing the target filespace name.
    matches: list[str] = []

    for line in result.stdout.splitlines():
        if FILESPACE_NAME in line:
            columns = line.split()

            if columns and columns[0].isdigit():
                matches.append(columns[0])

    # Do not guess if the target daemon is not running.
    if not matches:
        raise RuntimeError(
            f"No running LucidLink instance for "
            f"'{FILESPACE_NAME}' was found.\n"
            "The REST setup may be complete, but local ingest requires "
            "this filespace to be linked, mounted, and running. "
            "Start/link it using the LucidLink client, confirm it appears "
            "in `lucid list`, then rerun this script. "
            "The script will reuse the API resources."
        )

    # Do not guess if multiple instances match.
    if len(matches) > 1:
        raise RuntimeError(
            f"More than one daemon matched "
            f"'{FILESPACE_NAME}': {matches}. "
            "Refusing to guess which instance to operate on."
        )

    return matches[0]


# ---------------------------------------------------------------------------
# LUCIDLINK CLI COMMAND EXECUTION
# ---------------------------------------------------------------------------

def run_lucid(
    instance_id: str,
    command: str,
) -> str:
    """
    Run one LucidLink CLI command against a specific instance.

    Every command includes the selected instance ID so that an unrelated
    LucidLink filespace is not accidentally affected.
    """

    try:
        result = subprocess.run(
            [
                "lucid",
                "--instance",
                instance_id,
                command,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )

    except subprocess.CalledProcessError as exc:
        details = (
            exc.stderr or exc.stdout or ""
        ).strip()

        raise RuntimeError(
            f"`lucid --instance {instance_id} {command}` "
            f"failed: {details}"
        ) from None

    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise RuntimeError(
            f"Could not complete LucidLink command "
            f"'{command}': {exc}"
        ) from None

    # Print useful CLI output without exposing unrelated information.
    output = (result.stdout or "").strip()

    if output:
        print(output)

    return output


# ---------------------------------------------------------------------------
# UPLOAD BACKLOG PARSING
# ---------------------------------------------------------------------------

def parse_remaining_upload_bytes(
    cache_output: str,
) -> int:
    """
    Extract the Remaining upload value from `lucid cache`.

    Examples accepted:
        Remaining upload: 0B
        Remaining upload: 0 B
        Remaining upload: 1.5 MiB

    If the expected value is missing or unrecognized, stop rather than
    assuming that the upload is complete.
    """

    match = re.search(
        r"Remaining upload:\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*"
        r"(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)\b",
        cache_output,
        flags=re.IGNORECASE,
    )

    if not match:
        raise RuntimeError(
            "Could not find a recognizable 'Remaining upload' "
            "value in `lucid cache` output; refusing to shut down "
            "the daemon."
        )

    value = float(match.group(1))
    unit = match.group(2).lower()

    # Map each unit to its exponent.
    powers = {
        "b": 0,
        "kib": 1,
        "mib": 2,
        "gib": 3,
        "tib": 4,
        "kb": 1,
        "mb": 2,
        "gb": 3,
        "tb": 4,
    }

    # Use binary conversion for IEC units and decimal conversion
    # for units that do not include the "i".
    base = 1024 if "i" in unit else 1000

    return int(
        value * (base ** powers[unit])
    )


# ---------------------------------------------------------------------------
# SYNC AND UPLOAD VERIFICATION
# ---------------------------------------------------------------------------

def wait_for_uploads_to_finish(
    instance_id: str,
) -> None:
    """
    Synchronize the target filespace and wait for uploads to finish.

    The daemon is not shut down until the cache reports zero remaining
    upload bytes. If the timeout expires, the daemon stays running.
    """

    print(
        "[sync] Requesting LucidLink synchronization"
    )

    # Ask LucidLink to synchronize with the cloud.
    run_lucid(instance_id, "sync")

    # Use a monotonic clock for elapsed-time measurement.
    deadline = (
        time.monotonic() + UPLOAD_TIMEOUT_SECONDS
    )

    while True:
        # Query the cache for the current upload backlog.
        cache_output = run_lucid(
            instance_id,
            "cache",
        )

        remaining = parse_remaining_upload_bytes(
            cache_output
        )

        # Zero remaining bytes means the upload backlog is clear.
        if remaining == 0:
            print(
                "[sync] Remaining upload is 0 B."
            )
            return

        # Do not shut down if the upload timeout is reached.
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Timed out with {remaining} bytes still "
                "reported for upload. "
                "The daemon has NOT been shut down."
            )

        print(
            f"[sync] {remaining} bytes remain; "
            f"checking again in {UPLOAD_POLL_SECONDS} seconds..."
        )

        time.sleep(UPLOAD_POLL_SECONDS)


# ---------------------------------------------------------------------------
# COPY SAMPLE THROUGH THE MOUNTED FILESYSTEM
# ---------------------------------------------------------------------------

def copy_sample_to_mount() -> Path:
    """
    Copy the local sample asset into /Raw_Footage.

    This is a filesystem copy through the mounted LucidLink volume.
    It is not an API upload.
    """

    destination_dir = (
        MOUNT_PATH / "Raw_Footage"
    )

    # Confirm that the target filespace mount is accessible.
    if not MOUNT_PATH.is_dir():
        raise RuntimeError(
            f"Mount path is not accessible: {MOUNT_PATH}\n"
            "Link and mount the target filespace, then rerun the script."
        )

    # Confirm that the REST-created directory is visible in the mount.
    if not destination_dir.is_dir():
        raise RuntimeError(
            f"Expected directory is not accessible: {destination_dir}. "
            "Confirm the REST-created directory has appeared in the mount."
        )

    # Ensure the source sample exists.
    source = ensure_sample_asset()

    # Construct the destination path.
    destination = destination_dir / source.name

    # Copy the file while preserving basic file metadata.
    shutil.copy2(
        source,
        destination,
    )

    print(
        f"[ingest] Copied {source} -> {destination} "
        f"({destination.stat().st_size} bytes)"
    )

    return destination


# ---------------------------------------------------------------------------
# DAEMON SHUTDOWN VERIFICATION
# ---------------------------------------------------------------------------

def verify_instance_exited(
    instance_id: str,
) -> None:
    """
    Confirm that the target daemon is no longer listed.

    Other LucidLink instances are not targeted by this check.
    """

    result = subprocess.run(
        ["lucid", "list"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )

    # Check whether the target instance ID remains in the listing.
    for line in result.stdout.splitlines():
        columns = line.split()

        if columns and columns[0] == instance_id:
            raise RuntimeError(
                f"Instance {instance_id} still appears in "
                "`lucid list`; please inspect the daemon state manually."
            )

    print(
        f"[shutdown] Verified instance {instance_id} "
        "is no longer listed."
    )

    print(
        "[shutdown] Other LucidLink instances were not targeted."
    )


# ---------------------------------------------------------------------------
# MAIN WORKFLOW
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Run the complete project onboarding workflow.

    Order of operations:
    1. Validate credentials and configuration.
    2. Provision cloud resources.
    3. Configure membership and permissions.
    4. Create the target directory.
    5. Identify the correct running daemon.
    6. Copy the sample asset through the mount.
    7. Synchronize and verify the upload backlog.
    8. Exit only the target daemon.
    9. Verify that the target daemon has stopped.
    """

    # Check credentials before making any API changes.
    if not TOKEN:
        print(
            "ERROR: LL_TOKEN is not set. Copy .env.example to .env "
            "and add your service-account token to LL_TOKEN. "
            "Do not paste the token into chat or commit .env.",
            file=sys.stderr,
        )
        return 2

    # Validate the required project configuration.
    if (
        not FILESPACE_NAME
        or not MEMBER_EMAIL
        or not GROUP_NAME
    ):
        print(
            "ERROR: Filespace, member email, and group name "
            "must be non-empty.",
            file=sys.stderr,
        )
        return 2

    try:
        print("=== LucidLink project setup ===")
        print(f"API: {API_BASE_URL}")
        print(f"Filespace: {FILESPACE_NAME}")
        print(f"Member: {MEMBER_EMAIL}")
        print(f"Group: {GROUP_NAME}")

        # ---------------------------------------------------------------
        # PHASE 1: CLOUD RESOURCE PROVISIONING
        # ---------------------------------------------------------------

        # Find or create the filespace.
        filespace = get_filespace()
        filespace_id = str(filespace["id"])

        # Find or invite the member.
        member = get_or_invite_member()
        member_id = str(member["id"])

        # Find or create the group.
        group = get_or_create_group()
        group_id = str(group["id"])

        # Ensure the member belongs to the group.
        add_member_to_group(
            group_id,
            member_id,
        )

        # Ensure the group has read access to the filespace root.
        ensure_root_read_permission(
            filespace_id,
            group_id,
        )

        # Ensure the Raw_Footage directory exists.
        ensure_raw_footage_directory(
            filespace_id,
        )

        # ---------------------------------------------------------------
        # PHASE 2: LOCAL FILE INGEST
        # ---------------------------------------------------------------

        # Identify the daemon belonging to the target filespace.
        # Do not assume the first listed daemon is the correct one.
        instance_id = find_target_instance_id()

        # Copy the sample file through the mounted LucidLink filesystem.
        copy_sample_to_mount()

        # ---------------------------------------------------------------
        # PHASE 3: SYNCHRONIZATION AND SAFE SHUTDOWN
        # ---------------------------------------------------------------

        # Synchronize and verify zero remaining upload bytes.
        # If this fails or times out, the daemon is left running.
        wait_for_uploads_to_finish(
            instance_id,
        )

        # Exit only the daemon belonging to the target filespace.
        print(
            f"[shutdown] Exiting only target daemon instance "
            f"{instance_id}"
        )

        run_lucid(
            instance_id,
            "exit",
        )

        # Confirm that the target instance has stopped.
        verify_instance_exited(
            instance_id,
        )

        print(
            "\nSUCCESS: Project setup and safe shutdown completed."
        )

        return 0

    # Handle expected API, runtime, filesystem, and configuration errors.
    # Do not automatically delete resources if a later step fails.
    except (
        ApiError,
        RuntimeError,
        OSError,
        ValueError,
    ) as exc:

        print(
            f"\nERROR: {exc}",
            file=sys.stderr,
        )

        print(
            "The script stopped without deleting resources. "
            "If the REST setup already succeeded, rerunning it "
            "will reuse matching resources.",
            file=sys.stderr,
        )

        return 1


# ---------------------------------------------------------------------------
# SCRIPT ENTRY POINT
# ---------------------------------------------------------------------------

# This allows the file to be imported without automatically running main().
# When executed directly, main() runs and its return value becomes the
# process exit code.
if __name__ == "__main__":
    raise SystemExit(main())
