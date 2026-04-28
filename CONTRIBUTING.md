# Contributing to tamago

Thanks for your interest in making tamago better. 🥚

## What belongs here

Tamago is the **engine**, not the soul. Pull requests are welcome for:

- Bug fixes in `setup.py`, `scripts/`, `git-hooks/`
- Improvements to built-in skills (`skills/text-to-speech`, `skills/hatch`)
- Clearer documentation in `docs/`
- New templates under `templates/`
- Tests for `test_setup.py`

Out of scope for this repo:
- Persona files (those live in your private profile repo)
- Agent-specific memory content
- Machine-local config (`.tamago/machine.env`, `.tamago/machine.toml`)

## Development setup

```bash
git clone https://github.com/HammerMei/tamago
cd tamago
python3 -m pytest test_setup.py -v    # run existing tests
```

No extra dependencies — tamago is intentionally pure Python 3.9+ stdlib for `setup.py`,
and plain bash for scripts.

## Submitting changes

1. Fork the repo and create a branch from `main`
2. Make your changes and add/update tests as needed
3. Run `python3 -m pytest test_setup.py -v` — all tests must pass
4. Open a pull request with a clear description of what and why

## Code style

- Python: follow the existing style in `setup.py` (no external linter required)
- Shell: `set -euo pipefail`, quote variables, prefer `$HOME` over `~`
- Keep it minimal — tamago has zero runtime dependencies by design

## Reporting issues

Open a GitHub issue with:
- What you expected vs. what happened
- Your OS / Python version
- Relevant output from `bash ~/.tamago/scripts/health-check.sh`

## License

By contributing, you agree your changes will be licensed under the [MIT License](LICENSE).
