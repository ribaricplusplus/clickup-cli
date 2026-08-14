# Security Policy

## Supported versions

Until the first stable release, security fixes are made on the latest release line only.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature for this repository. Do not open a
public issue containing exploit details, credentials, task data, workspace identifiers, or other
sensitive information.

Include the affected version, impact, reproduction steps using synthetic data, and any suggested
mitigation. Maintainers will acknowledge a complete report as soon as practical and coordinate a
fix and disclosure timeline.

## Credential handling

The CLI intentionally has no `--token` option. Tokens are read from `CLICKUP_API_TOKEN` or a
non-executable dotenv file, sent as the raw ClickUp `Authorization` value, redacted from expected
errors, and never included in normal output. Users remain responsible for restricting env-file
permissions, using sandbox Lists for testing, and rotating a token that may have been exposed.
Custom non-local API bases must use HTTPS; plaintext HTTP is accepted only for localhost tests.
The client disables ambient HTTP proxy discovery so a raw ClickUp token cannot be redirected by
`HTTP_PROXY`, `HTTPS_PROXY`, or related process configuration.
