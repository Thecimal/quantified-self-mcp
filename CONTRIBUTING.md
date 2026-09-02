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

Install the required dependencies:

```bash
pip install -r requirements.txt
```

Run the available tests before submitting changes.

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

### Data Safety

Contributors should not introduce unexpected modification of user data.

Any future write functionality should require explicit discussion, documentation,
and security review.

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
