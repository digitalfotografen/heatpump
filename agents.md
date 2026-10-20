# Agent for this project
A Raspberry Pi controller that pauses a Rego 637 heat pump through its EXT input when
electricity is expensive or the wood stove is burning. `docs/plan.md` is the implementation
plan; work on **one phase at a time** and read the phase and §2 principles before starting.

The Raspberry Pi is accessed via SSH, configured in the PyCharm project. The SSH user and
password are given in the prompts; ask the human developer if forgotten.
The remote project folder is `/home/[user]/heatpump`. Configuration is in `/etc/heatpump/`,
data in `/var/lib/heatpump/`.
Don't overwrite the whole remote project or its `.venv`. Only send files that changed.

Development is done in steps and the human developer decides which. **Commit only after the
developer has reviewed the phase.** Don't change code unrelated to the task at hand, and keep
the codebase clean and maintainable.

**Safety:** the relay must never be connected to EXT by the agent (phase 12 is done by hand).
Anything that widens the relay's reach (new pause reasons, longer caps, removed guard rules)
needs the developer's decision. See §10 of the plan.

# Coding Guidelines
For every function you write:
- Prefer stdlib over custom logic
- Use type hints (Python 3.13 style)
- Raise specific exceptions, not generic ones
- Write one-line docstrings for non-obvious functions
- Avoid reinventing: file I/O, path manipulation, HTTP requests, date parsing, data validation,
  JSON handling, or retry logic

## Python and Environment
- **Python 3.13**, managed by `uv` (`uv run heatpump ...`, `uv run pytest`).
- Follow [PEP 8](https://peps.python.org/pep-0008/); name things so comments are rarely needed.
- Type hints everywhere.
- Test driven development: write the tests before the implementation, keep them up to date.
- `ruff` for linting and formatting after each iteration (`uv run ruff format . && uv run ruff check .`).
- reStructuredText docstrings (`:param x:`, `:raises E:`), as JetBrains writes them.
- Planner and guard are pure functions; all I/O lives in the I/O modules (plan §2).
- UTC in storage; Europe/Stockholm only when printing or for "after 14:00 local".

## Package management
`uv` and `pyproject.toml`; list dependencies there, dev tools in the `dev` group.
Ask before adding dependencies that are not named in the plan.

## Testing
- `pytest`, with `pytest-asyncio` for async code (`asyncio_mode = "auto"`).
- Hardware is replaced by fakes (`driver: dry-run`, a fake I2C bus); tests must pass on the dev
  machine without the HAT. Hardware checks are run on the Pi.
- All tests pass before a phase is reported done.
