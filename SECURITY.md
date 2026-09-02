# Security Policy

## Supported Versions

Security fixes are generally provided for the latest version of Quantified Self MCP.

If you are using an older version, please upgrade to the latest release before
reporting an issue whenever possible.

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Older releases | Best effort |

## Reporting a Vulnerability

Please do **not** report security vulnerabilities through public GitHub issues.

If you discover a security vulnerability, please use GitHub's private
vulnerability reporting feature for this repository when available.

If private reporting is not available, contact the project maintainer privately
through the contact method listed in the repository profile.

Please include:

- A description of the vulnerability.
- Steps to reproduce it.
- Potential impact.
- A proof of concept, if safe to provide.
- Suggested mitigation, if available.

## Please Do Not Include

Do not publicly share:

- Real user health data.
- Personal SQLite databases.
- API keys or access tokens.
- Passwords.
- Private keys.
- Personally identifiable information.
- Sensitive production data.

If a proof of concept requires sensitive data, use synthetic or anonymized data
whenever possible.

## Scope

Security issues may include:

- Unauthorized access to local data.
- Path traversal vulnerabilities.
- SQL injection.
- Unsafe database access.
- Accidental data modification.
- Information disclosure.
- Credential exposure.
- Unsafe MCP tool behavior.
- Unexpected network communication.
- Dependency vulnerabilities with meaningful impact on users.

## Response Process

After receiving a valid vulnerability report, the maintainer will make a
reasonable effort to:

1. Confirm receipt of the report.
2. Investigate the vulnerability.
3. Determine the severity and affected versions.
