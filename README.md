# CoderLand Experiment Skill

An installable Codex Skill and small Python helper for working with the CoderLand experiment space. It can inspect and package user-selected datasets, upload dataset archives through the BUU route, submit reproduction files, check evaluation results, and manage the current account's own records.

## What is included

- `SKILL.md` — when to use the Skill, operating rules, supported workflows, and safety boundaries.
- `scripts/coderland.py` — Python 3 standard-library helper for credentials, live contract discovery, archive validation, packaging, uploads, and API calls.

The Skill reads the live API contract and upload profiles for current request schemas and limits. If the API reports `410 FEATURE_DISABLED`, stop and wait until the service is available; do not use the website's session-only routes as a workaround.

## Install in Codex

Once this public repository is available, paste this prompt into Codex:

> Install the CoderLand Experiment Skill from `https://github.com/littlezuicat/coderland-experiment-skill/tree/main/coderland-experiment` using the `skill-installer`.

The installer places it in `~/.codex/skills/coderland-experiment`. Restart or refresh Codex's skill discovery after installing. If you use a custom Codex skills directory, follow the installer's destination option.

To install manually, copy the contents of `coderland-experiment/` (including `SKILL.md` and `scripts/`) into a directory named `coderland-experiment` under your Codex skills directory.

## First use

1. Sign in to CoderLand and generate an Experiment API Key in Personal Center → Account Settings.
2. Run `python3 ~/.codex/skills/coderland-experiment/scripts/coderland.py configure`. Enter the key at the hidden prompt; it will not be echoed.
3. Run `python3 ~/.codex/skills/coderland-experiment/scripts/coderland.py contract` to fetch the current rules and upload profiles before a task.
4. Keep the key private. It is stored in the operating-system config directory with restrictive file permissions. Use `forget` to remove the local copy and revoke the key in CoderLand to invalidate it.

## Common commands

```bash
python3 ~/.codex/skills/coderland-experiment/scripts/coderland.py --help
python3 ~/.codex/skills/coderland-experiment/scripts/coderland.py contract
python3 ~/.codex/skills/coderland-experiment/scripts/coderland.py api GET /api/v1/experiments
python3 ~/.codex/skills/coderland-experiment/scripts/coderland.py inspect --profile paired-restoration --source ./dataset.zip
python3 ~/.codex/skills/coderland-experiment/scripts/coderland.py package --profile paired-restoration --source ./dataset-folder
python3 ~/.codex/skills/coderland-experiment/scripts/coderland.py upload-dataset --profile paired-restoration --category 去雾 \
  --name 示例数据 --description 室外去雾 --test ./coderland-output/dataset-paired-restoration.zip
```

Generated packages go under the current project's `coderland-output/`. The helper preserves input image and model-weight bytes; profile-permitted archive path normalization is limited to the new ZIP.

## Upload network

File transfers require the API's BUU network check to report `available: true`. Dataset uploads use short-lived file-scoped tickets and sequential resumable chunks. Reproduction files are streamed through the BUU route. If BUU is unavailable, stop before creating an upload or submitting a record.

## Safety boundaries

- Work only with the dataset, paper, and files selected by the user. Never infer a target from filenames or directory contents.
- Keep API keys, BUU tickets, temporary upload URLs, and credentials out of chat, logs, shell arguments, and public commits.
- Ask for explicit confirmation immediately before a permanent deletion. Do not delete any data unless asked.
- Do not invent paper metadata, experiment settings, or evaluation scores.
- The helper is limited to documented `/api/v1/experiments` member operations. It does not perform admin actions.

See `SKILL.md` for detailed task routing, data-layout profiles, recovery steps, and failure handling. The live OpenAPI document is the final source for endpoint schemas and current restrictions.
