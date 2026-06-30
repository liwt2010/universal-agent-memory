# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

We take security seriously. If you discover a security vulnerability in UAMS, please report it responsibly.

### How to Report

**Please do NOT report security vulnerabilities through public GitHub issues.**

Instead, please report security issues by emailing the maintainers directly at:

📧 **security@uams.dev** (placeholder — update with actual contact)

Please include:
- A description of the vulnerability
- Steps to reproduce (if applicable)
- The affected version(s)
- Any potential impact or severity assessment
- Your contact information (if you wish to be credited)

### Response Timeline

- **Acknowledgment**: Within 48 hours of receiving your report
- **Initial assessment**: Within 7 days
- **Fix timeline**: Depends on severity, typically 14-30 days
- **Public disclosure**: We will coordinate with you on the timing and details of any public disclosure

### What to Expect

1. We will confirm receipt of your report within 48 hours
2. We will investigate and validate the vulnerability
3. We will develop and test a fix
4. We will release the fix and notify you
5. We will publicly disclose the issue (with your consent for attribution) after the fix is available

## Security Features in UAMS

UAMS includes several built-in security features:

- **Input Sanitization**: SQL injection and XSS prevention via `InputValidator`
- **Rate Limiting**: Sliding window rate limiting to prevent abuse
- **Privacy Filter**: Automatic PII and secret detection/redaction
- **Input Length Limits**: Configurable maximum input lengths (default 10,000 chars)
- **SQL Injection Protection**: Dangerous SQL keywords and characters are stripped from all text inputs

## Security Best Practices

When deploying UAMS in production:

1. **Use persistent storage backends** (SQLite, PostgreSQL, Redis) rather than in-memory for production
2. **Enable input validation** via `UAMSConfig.validate()`
3. **Set rate limits** appropriate for your use case
4. **Review and configure the Privacy Filter** for your domain-specific secrets
5. **Keep dependencies up to date** via `pip install --upgrade`
6. **Use HTTPS** for any network-based storage backends
7. **Configure proper authentication** for PostgreSQL, Redis, and Neo4j backends

## Known Security Considerations

- **Memory storage**: `InMemoryStore` does not persist data across process restarts. Use SQLite or PostgreSQL for production.
- **Embedding data**: Embeddings contain semantic information. Ensure they are stored securely in production environments.
- **Multi-agent signals**: Signal payloads are not encrypted by default. Use TLS for network-based signal backends.
