# LucidLink Project Setup Homework

This folder contains a commented Python script and local configuration template.

## **Files**

- `setup_project.py`: Automates Scenario 2 provisioning through the REST API, mounted-file ingest, upload monitoring, and safe exit of the target LucidLink daemon. Includes inline comments explaining the code and safety checks.
- `Scenario1.txt`: Documents the completed Scenario 1 investigation, including login/logout timestamps, local settings changes, and clean versus ungraceful daemon exits.
- `scenario2_curl_setup.md`: Documents the Scenario 2 REST API workflow using individual `curl` commands, with comments explaining what each command does and why it is needed.
- `.env.example`: Configuration template showing the required environment variables. Copy it to `.env` and set `LL_TOKEN` to your service-account token.
- `.gitignore`: Excludes the real `.env` file, Python cache files, and other local artifacts from Git.
- `sample-assets/sample-footage.txt`: A small sample asset used to test copying a file into the mounted LucidLink filespace and verifying upload completion.
- `README.md`: Project overview, setup instructions, file descriptions, and guidance for running the workflow.

## Requirements

- macOS with the LucidLink CLI installed and available as `lucid`.
- The LucidLink API container running at `http://localhost:3003` (or update `LL_API_BASE_URL`).
- A valid service-account token with permission to manage filespaces, members, groups, permissions, and entries.
- The target filespace linked and mounted in the LucidLink client before the ingest stage. The script will stop rather than guess CLI link arguments if the daemon is not running.

## Configure

From this folder:

```bash
cp .env.example .env
```

Edit `.env` and replace the placeholder `LL_TOKEN` with the service-account token. Do not commit `.env`. The script reads `.env` without requiring `python-dotenv`.

Check that `.gitignore` includes `.env`. If you use Git, run `git status --short` before committing.

## Run

```bash
python3 setup_project.py
```

The script checks for existing filespaces, members, groups, memberships, permissions, and the `Raw_Footage` directory before creating them. This makes a rerun safer after a partial run.

The script copies the configured sample file into:

`/Volumes/jozefs-space/project-alpha-2026/Raw_Footage/`

It then runs `lucid --instance <id> sync`, polls the CLI cache output until `Remaining upload` is zero, and exits only the instance whose listed filespace name matches `LL_FILESPACE_NAME`.

## Important behavior

- It does not delete filespaces, members, groups, permissions, or files if a step fails. This script deliberately avoids destructive automatic rollback.
- It does not print the service-account token or invitation link.
- It will not shut down the daemon if the upload counter is missing, non-zero past timeout, or cannot be parsed.
- It refuses to continue with local ingest if the target daemon is not listed by `lucid list`.
- The script defaults are based on the test environment used during the homework. Adjust `.env` for a different workspace, mount path, region, or sample file.
