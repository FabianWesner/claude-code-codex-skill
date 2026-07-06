# Claude Code Codex Skill

Claude Code skill for running headless Codex as an orchestration sub-agent. It packages a `codex-subagent` skill plus helper scripts for one-shot Codex runs and steerable Codex app-server sessions.

## What Is Included

- `.claude/skills/codex-subagent/SKILL.md` documents the Claude Code skill.
- `.claude/skills/codex-subagent/scripts/codex_appserver.py` runs a single Codex app-server turn.
- `.claude/skills/codex-subagent/scripts/codex_session.py` runs an interactive file-controlled Codex session.

## Requirements

- Claude Code with local skill support.
- OpenAI Codex CLI installed and authenticated.
- Python 3.9 or newer.

The bundled recipes assume the Codex CLI is available as `codex` and that authentication is already configured for the local user.

## Installation

Copy or symlink the skill directory into your Claude skills directory:

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/.claude/skills/codex-subagent" ~/.claude/skills/codex-subagent
```

Restart Claude Code after installing the skill so it can discover the new skill metadata.

## Usage

Open the skill file for the full operating rules and verified command patterns:

```bash
cat .claude/skills/codex-subagent/SKILL.md
```

The skill covers:

- read-only Codex second opinions
- bounded workspace-write coding tasks
- resuming Codex sessions
- steerable app-server sessions with a file-based control plane

## License

MIT License. See `LICENSE`.
