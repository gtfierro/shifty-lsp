# shifty-lsp build process
#
# Local dev:
#   make dev        # set up .venv and install the package (editable)
#   make test       # run the end-to-end LSP test
#   make build      # build the Python wheel + sdist and the VS Code .vsix
#   make clean      # remove all build artifacts

PYTHON     ?= python3
VENV       := .venv
VENV_PY    := $(VENV)/bin/python
VSCODE_DIR := editors/vscode
VERSION    := $(shell grep -m1 '^version' pyproject.toml | cut -d'"' -f2)
VSIX       := $(VSCODE_DIR)/shifty-lsp-$(VERSION).vsix

.PHONY: all dev test build wheel vsix lint clean publish

all: build

# --- local development -----------------------------------------------------

$(VENV_PY):
	uv venv $(VENV)

dev: $(VENV_PY)
	uv pip install -e .

test: dev
	$(VENV_PY) test_e2e.py

# --- build artifacts ---------------------------------------------------------

build: wheel vsix

wheel: dev
	rm -rf dist
	uv build

$(VSCODE_DIR)/node_modules: $(VSCODE_DIR)/package.json
	cd $(VSCODE_DIR) && npm install --no-fund --no-audit
	@touch $@

vsix: $(VSIX)

$(VSIX): $(VSCODE_DIR)/node_modules $(VSCODE_DIR)/extension.js $(VSCODE_DIR)/package.json
	cd $(VSCODE_DIR) && npx @vscode/vsce package --allow-missing-repository

# --- release -----------------------------------------------------------------

publish: build
	uv publish
	@echo "Don't forget: cd $(VSCODE_DIR) && npx @vscode/vsce publish"

clean:
	rm -rf dist build *.egg-info $(VSCODE_DIR)/node_modules $(VSCODE_DIR)/*.vsix
