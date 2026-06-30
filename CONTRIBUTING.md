# Contributing to UAMS

Thank you for your interest in contributing to the Universal Agent Memory System (UAMS)! This document provides guidelines and instructions for contributing.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [How to Contribute](#how-to-contribute)
- [Pull Request Process](#pull-request-process)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Documentation](#documentation)
- [Release Process](#release-process)

## Code of Conduct

This project and everyone participating in it is governed by our [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

## Getting Started

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/universal-agent-memory.git
   cd universal-agent-memory
   ```
3. **Add the upstream remote**:
   ```bash
   git remote add upstream https://github.com/uams/universal-agent-memory.git
   ```
4. **Create a virtual environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

## Development Setup

```bash
# Install in editable mode with all dev dependencies
pip install -e ".[dev]"

# Install optional backend dependencies
pip install chromadb redis neo4j psycopg2-binary tiktoken sentence-transformers

# Run the test suite
python -m unittest discover -s tests -v

# Run with coverage
python -m coverage run -m unittest discover -s tests
python -m coverage report -m
```

## How to Contribute

### Reporting Bugs

Before creating a bug report, please:

1. Check the [existing issues](https://github.com/uams/universal-agent-memory/issues) to avoid duplicates
2. Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md)

Include:
- A clear title and description
- Steps to reproduce the issue
- Expected vs actual behavior
- Python version and environment info
- Relevant code snippets or error traces

### Suggesting Features

1. Check [existing issues](https://github.com/uams/universal-agent-memory/issues) for similar suggestions
2. Use the [feature request template](.github/ISSUE_TEMPLATE/feature_request.md)
3. Describe the use case and how it benefits the project

### Contributing Code

1. Create a new branch for your feature:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. Make your changes with clear, focused commits
3. Ensure all tests pass
4. Update documentation if needed
5. Submit a pull request

## Pull Request Process

1. Update the [CHANGELOG.md](CHANGELOG.md) with details of your changes
2. Ensure all tests pass: `python -m unittest discover -s tests -v`
3. Update documentation for any new features or API changes
4. Link any related issues in the PR description
5. Wait for review from maintainers

## Coding Standards

### Python Style

- Follow [PEP 8](https://pep8.org/) style guide
- Use [Black](https://black.readthedocs.io/) for formatting: `black src/ tests/`
- Use type hints where possible
- Maximum line length: 100 characters

### Docstrings

All public classes and methods should have docstrings following the Google style:

```python
def my_function(param1: str, param2: int) -> bool:
    """Brief description of the function.
    
    Longer description if needed, explaining the purpose,
    behavior, and any important details.
    
    Args:
        param1: Description of param1
        param2: Description of param2
        
    Returns:
        Description of the return value
        
    Raises:
        ValueError: When param2 is negative
    """
```

### Commit Messages

Use clear, descriptive commit messages following conventional commits:

```
feat: add PostgreSQL storage backend
fix: resolve memory leak in MetricsCollector
docs: update API documentation for recall()
test: add benchmark suite for delete operations
refactor: simplify retry configuration handling
```

## Testing

### Test Coverage

Aim for high test coverage, especially for:
- Critical paths (store, retrieve, recall)
- Error handling and graceful degradation
- Thread safety and concurrency
- Edge cases (empty inputs, None values, boundary conditions)

### Writing Tests

```python
import unittest
from uams.storage.memory import InMemoryStore
from uams.core.models import Memory, MemoryId, AgentContext, MemoryPayload, MemoryMetadata
from uams.core.enums import MemoryType, PrivacyLevel

class TestMyFeature(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore(max_capacity=100)
    
    def test_something_specific(self):
        # Arrange
        mem = self._make_memory()
        
        # Act
        self.store.store(mem)
        result = self.store.retrieve(mem.id)
        
        # Assert
        self.assertIsNotNone(result)
        self.assertEqual(result.payload.raw, "test content")
    
    def _make_memory(self, raw="test content"):
        return Memory(
            id=MemoryId(),
            anchor=TemporalAnchor(),
            context=AgentContext(agent_id="a", agent_type="t", session_id="s"),
            payload=MemoryPayload(raw=raw),
            metadata=MemoryMetadata(
                memory_type=MemoryType.WORKING,
                privacy=PrivacyLevel.PUBLIC,
                importance=5.0,
                confidence=1.0,
            ),
        )
```

### Running Tests

```bash
# All tests
python -m unittest discover -s tests -v

# Specific test file
python -m unittest tests.test_system -v

# Specific test case
python -m unittest tests.test_system.TestUniversalMemorySystem.test_observe_and_recall -v

# With pytest (if installed)
pytest tests/ -v --tb=short
```

## Documentation

- Keep README.md updated with any new features or API changes
- Update docstrings for all public APIs
- Add examples to `examples/` for new features
- Update deployment docs if backend behavior changes

### Documentation Structure

- `README.md` — Main project overview (English)
- `README.zh-CN.md` — Chinese Simplified version
- `README.zh-TW.md` — Chinese Traditional version
- `docs/DEPLOYMENT.md` — Deployment guide (English)
- `docs/DEPLOYMENT.zh-CN.md` — Deployment guide (Chinese)
- `docs/API.md` — API reference documentation
- `docs/ARCHITECTURE.md` — Architecture and design documentation

## Release Process

1. Update version in `pyproject.toml`
2. Update `CHANGELOG.md` with release date and summary
3. Create a GitHub release with release notes
4. Tag the release: `git tag -a v0.1.0 -m "Release v0.1.0"`
5. Push tags: `git push origin --tags`

## Questions?

- Join our [GitHub Discussions](https://github.com/uams/universal-agent-memory/discussions)
- Open an issue with the [question template](.github/ISSUE_TEMPLATE/question.md)
- Check existing documentation in `docs/`

Thank you for contributing to UAMS! 🚀
