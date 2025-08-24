# findpy – a find-like filesystem traversal CLI

findpy is a fast, find-like CLI implemented in pure Python. It traverses directories and filters
results by name, type, size, depth, emptiness, and more.

It was generated for evaluation, and lives alongside the original PMAT scaffolding.

## Install (editable dev)

Use a virtual environment and install dev tools:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
pre-commit install
```

## Usage

```bash
findpy [PATH ...] [options]
```

Common options:

- -name PATTERN            Glob pattern (case-sensitive)
- -iname PATTERN           Glob pattern (case-insensitive)
- -type {f,d,l}            Filter by type: file, dir, symlink
- -maxdepth N              Descend at most N levels (0 = roots only)
- -mindepth N              Skip entries shallower than N
- -size [+|-]N[UNIT]       Size filter; UNIT: b,k,m,g,t (powers of 1024)
- -empty                   Empty files or directories
- --follow                 Follow symlinked directories
- --ignore-hidden          Skip dotfiles and dot-directories
- --exclude PATTERN        Exclude names (repeatable)
- --exclude-dir PATTERN    Exclude directories from descent (repeatable)
- -print0                  NUL-separated output

Examples:

```bash
# All *.py files below src
findpy src -type f -name "*.py"

# Case-insensitive name match, excluding .venv
findpy . -iname "*.md" --exclude-dir .venv

# Files bigger than 5 MB
findpy . -type f -size +5m

# Only empty directories at depth 2
findpy . -type d -empty -mindepth 2 -maxdepth 3
```

## Development

Run linters and tests:

```bash
ruff check . && ruff format . && black . && mypy .
pytest -q
```

## License

MIT

---

Original repository context for PMAT evaluation is kept below.

---

# GPT 5 Model Evaluation

This repository holds the code output of GPT 5, used for evaluation with [PMAT](https://github.com/paiml/paiml-mcp-agent-toolkit).

Evaluations are posted at [Pragmatic AI Labs](https://paiml.com/models).

For details on the prompt used, check the [test.yaml](./test.yaml) file.


> [!NOTE] This repository does not accept Pull Requests
# GPT 5 Model Evaluation

This repository holds the code output of GPT 5, used for evaluation with [PMAT](https://github.com/paiml/paiml-mcp-agent-toolkit).

Evaluations are posted at [Pragmatic AI Labs](https://paiml.com/models).

For details on the prompt used, check the [test.yaml](./test.yaml) file.


> [!NOTE] This repository does not accept Pull Requests
