# Contributing to Quantified Self MCP

Thank you for your interest in contributing to Quantified Self MCP.

This project aims to provide a privacy-first, local MCP server for querying
personal data through AI assistants while keeping users in control of their data.

We welcome bug reports, documentation improvements, tests, feature ideas, and
code contributions.

## Before You Start

Before making significant changes:

1. Check existing issues and pull requests.
2. Open an issue to discuss large features or architectural changes.
3. Make sure your contribution aligns with the project's privacy-first and
   local-first principles.

For small fixes, documentation improvements, and test improvements, you can
usually open a pull request directly.

## Development Setup

Clone the repository:

```bash
git clone https://github.com/Thecimal/quantified-self-mcp.git
cd quantified-self-mcp
```

Create and activate your development environment according to the installation
instructions in the README.

Install the development dependencies (this also pulls in `requirements.txt`,
plus `pytest`, `ruff`, and the other tools needed to actually run the checks
below — `pip install -r requirements.txt` alone is not enough to develop or
test the project):

```bash
pip install -r requirements-dev.txt
```

Run the test suite:

```bash
pytest
```

Run the linter (CI runs this too — a pull request won't pass CI if this
doesn't):

```bash
ruff check .
```

To try the server against real sample data, generate or regenerate it with
`python sample_data/generate_sample_data.py` (see that script's docstring —
running it with no arguments reproduces the checked-in sample files exactly,
so this is safe to run any time), then follow the "Installation" steps in
the README.

## Project Principles

### Privacy First

Do not introduce unnecessary telemetry, tracking, analytics, or network
communication.

Personal user data should remain under the user's control.

### Local First

The project should continue to work locally without requiring cloud
infrastructure unless an optional integration explicitly documents otherwise.

### Security

Do not commit:

- API keys
- Access tokens
- Passwords
- Private keys
- Personal databases
- Personal health data
- Real user datasets

Use synthetic or anonymized sample data for tests and examples.

### Extending Import Support

Adding support for a new health-data export format (Fitbit, Google Fit,
etc.) doesn't need changes anywhere else: write one function in
`import_adapters.py` matching the existing `adapt_apple_health` pattern,
and register it in `ADAPTERS`. `init_db.py`'s CLI, and the
validate/upsert pipeline it shares with every adapter, pick it up
automatically.

### Data Safety

The server already includes write tools (`log_daily_metric`, `clear_metric`),
so this isn't hypothetical: changes to how they validate input, handle
errors, or decide what a tool call is allowed to touch need real care.
Don't broaden what a tool can write or read without a clear reason, and
call it out explicitly in the pull request description. New *kinds* of
write access (e.g. deleting rather than clearing/upserting) should be
discussed in an issue first.

## Making Changes

1. Fork the repository.
2. Create a new branch.
3. Make your changes.
4. Add or update tests where appropriate.
5. Update documentation if user-facing behavior changes.
6. Run the test suite.
7. Commit your changes with a clear commit message.
8. Open a pull request.

## Code Quality

Please aim for contributions that are:

- Clear and readable.
- Focused on a single purpose.
- Covered by tests when practical.
- Documented when behavior changes.
- Compatible with supported Python versions.
- Consistent with the existing project architecture.

Avoid unrelated refactoring in the same pull request.

## Testing

Before opening a pull request, verify that:

- Existing tests still pass.
- New functionality is tested when appropriate.
- Sample data does not contain real private information.
