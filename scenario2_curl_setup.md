# Scenario 2 — API setup and `curl` command notes

This file records the API setup flow for the LucidLink homework project. Commands use the local API container at `http://localhost:3003`. Run them from macOS Terminal after setting the environment variables below.

**Scope note:** The available terminal transcript explicitly shows the health check failing before the API container was started. The commands below include that check and the documented REST calls needed for the setup workflow. Resource creation calls are reproducible templates based on the current `setup_project.py` and supplied API reference; they are not represented as a verbatim transcript of every command previously typed. Replace IDs/placeholders with values returned by your own API responses. Never put a real token into a saved script or commit it.

## 1. Set local shell variables

```bash
# Base URL of the local LucidLink API container.
export LL_API='http://localhost:3003/api/v1'

# Load the token from your private .env file without printing it.
# Run this from the project directory; do not share the .env contents.
set -a
source .env
set +a
export LL_TOKEN
```

Why: curl needs the API base URL and bearer token. The token belongs in the HTTP Authorization header, not in a URL or a committed file.

## 2. Check whether the API is reachable

```bash
# Health is explicitly unauthenticated; a 200 response means the API is responding.
curl -i "$LL_API/health"
```

Observed first attempt in the terminal transcript:

```bash
curl -i http://localhost:3003/api/v1/health
```

It failed with `curl: (7) Failed to connect to localhost port 3003`, meaning no service was listening on that host port at that moment. After starting the container, repeat the health check.

## 3. Start the local API container (if it is not already running)

```bash
# Run the API in the background and publish container port 3003 on localhost:3003.
docker run -d --name lucidlink-api -p 3003:3003 lucidlink/lucidlink-api:latest

# Confirm Docker reports the container and port mapping.
docker ps --filter name=lucidlink-api

# Confirm the service responds after startup.
curl -i "$LL_API/health"
```

Expected health response from the observed setup was `HTTP/1.1 200 OK` with `{}`. If Docker says the name is already in use, inspect that container instead of blindly creating a second one.

## 4. Open the API documentation

```bash
# Open the API's documented Swagger UI route in a browser.
open http://localhost:3003/api/v1/docs

# This path was tried but is not a valid GET route in this API build.
curl -i http://localhost:3003/api-json
```

The `/api-json` request returned `Cannot GET /api-json`; the docs route identified during setup was `/api/v1/docs`.

## 5. Authenticate and inspect existing filespaces

```bash
# List filespaces before creating one, so a rerun does not duplicate a resource.
curl -sS -i "$LL_API/filespaces" \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Accept: application/json'
```

## 6. Create the filespace only if it does not already exist

```bash
# Create the target filespace. Use the required name/region for the assignment.
curl -sS -i -X POST "$LL_API/filespaces" \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -d '{
    "name": "project-alpha-2026",
    "region": "us-west-1",
    "storageOwner": "lucidlink",
    "storageProvider": "AWS"
  }'
```

Why: `POST /filespaces` creates the storage volume. Save the returned `data.id` as the filespace ID for the later permission and entry requests. Do not rerun POST blindly if the filespace already exists.

## 7. Look up a workspace member, or invite them if missing

```bash
# Search by email first; the email query prevents an unnecessary duplicate invite.
curl -sS -i --get "$LL_API/members" \
  --data-urlencode 'email=jerichosense@gmail.com' \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Accept: application/json'
```

```bash
# Invite only if the lookup confirms the member is absent.
curl -sS -i -X POST "$LL_API/members" \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -d '{"email":"jerichosense@gmail.com"}'
```

Why: the member ID returned by the lookup/invite is needed to add the member to a group. Treat invitation links or secrets in responses as private; do not paste them into shared notes.

## 8. Look up a group, or create it if missing

```bash
# Find the configured group before creating it.
curl -sS -i --get "$LL_API/groups" \
  --data-urlencode 'name=Video_Editors' \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Accept: application/json'
```

```bash
# Create the group only if the preceding lookup shows it is absent.
curl -sS -i -X POST "$LL_API/groups" \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -d '{"name":"Video_Editors"}'
```

Why: the group is the principal that receives the filespace permission. Save its returned ID.

## 9. Verify membership, then add the member to the group

```bash
# Inspect the group's current members before attempting the relationship change.
curl -sS -i "$LL_API/groups/GROUP_ID/members" \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Accept: application/json'
```

```bash
# Add one member to the group; replace both placeholders with returned IDs.
curl -sS -i -X PUT "$LL_API/groups/GROUP_ID/members/MEMBER_ID" \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Accept: application/json'
```

Why: membership is a separate relationship from inviting a member or creating a group. The documented endpoint uses the IDs in the URL; the current setup script sends no request body.

## 10. Inspect permissions, then grant group read access to the filespace root

```bash
# Filter existing permission entries to the target group.
curl -sS -i --get "$LL_API/filespaces/FILESPACE_ID/permissions" \
  --data-urlencode 'principalId=GROUP_ID' \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Accept: application/json'
```

```bash
# Grant read access to / only if that exact root permission is missing.
curl -sS -i -X POST "$LL_API/filespaces/FILESPACE_ID/permissions" \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -d '{
    "path": "/",
    "permissions": ["read"],
    "principalId": "GROUP_ID"
  }'
```

Why: the permission is scoped to the filespace ID, the group principal ID, and the path `/`. Verify the existing grant before creating another one.

## 11. Resolve the root and `Raw_Footage` path

```bash
# Resolve / to get its entry ID; creation uses the returned parentId.
curl -sS -i --get "$LL_API/filespaces/FILESPACE_ID/entries/resolve" \
  --data-urlencode 'path=/' \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Accept: application/json'
```

```bash
# Check whether /Raw_Footage already exists before creating it.
curl -sS -i --get "$LL_API/filespaces/FILESPACE_ID/entries/resolve" \
  --data-urlencode 'path=/Raw_Footage' \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Accept: application/json'
```

```bash
# Create the directory only if the previous lookup confirms it is missing.
# Replace ROOT_ENTRY_ID with the id returned when resolving path=/.
curl -sS -i -X POST "$LL_API/filespaces/FILESPACE_ID/entries" \
  -H "Authorization: Bearer $LL_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -d '{
    "parentId": "ROOT_ENTRY_ID",
    "name": "Raw_Footage",
    "type": "dir"
  }'
```

Why: the API creates the directory entry, but it does not copy local file bytes into the mounted volume. The Python script handles that local copy after confirming the matching LucidLink client instance is running.

## 12. What happens after the curl/API stage

The current Python workflow then:

1. Confirms the target filespace's daemon appears in `lucid list`.
2. Copies `sample-assets/sample-footage.txt` into the mounted `Raw_Footage` directory.
3. Runs `lucid --instance INSTANCE_ID sync`.
4. Polls `lucid --instance INSTANCE_ID cache` until `Remaining upload` is exactly zero, or stops on timeout/unparseable output.
5. Runs `lucid --instance INSTANCE_ID exit` only after upload completion, then verifies that instance is no longer listed.

These final steps are LucidLink CLI commands invoked by Python, not curl/API requests.

## Important accuracy and safety notes

- Replace `FILESPACE_ID`, `GROUP_ID`, `MEMBER_ID`, and `ROOT_ENTRY_ID` with actual IDs returned by the API.
- The setup script's configured defaults are `project-alpha-2026`, `us-west-1`, `jerichosense@gmail.com`, and `Video_Editors`. Change them only if the assignment says otherwise.
- `curl -i` prints response headers as well as the body, which is useful for seeing HTTP status codes.
- Do not paste the service-account token into this document. Use `.env` and the `$LL_TOKEN` variable.
- The Scenario.txt file was not present among the files available in the current runtime when this guide was prepared. Therefore, verify the exact Scenario 2 names/requirements against that file before treating the defaults above as final assignment values.
