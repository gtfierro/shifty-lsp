# shifty-lsp

A language server for Turtle/RDF ontology files, combining:

- **[ontoenv](https://ontoenv.gtf.fyi)** — manages an ontology environment rooted at the
  enclosing Git repository. All `*.ttl` files in the repo are discovered automatically, and
  `owl:imports` declarations are resolved and downloaded into a local cache.
- **[shifty](https://shifty.gtf.fyi)** — continuous SHACL validation + SHACL-AF inference.
  The document is validated against the transitive closure of its `owl:imports` (used as the
  shapes graph). `sh:Violation`, `sh:Warning`, and `sh:Info` results are published as LSP
  error/warning/information diagnostics.

## Install

```sh
pip install -e .
```

This provides the `shifty-lsp` executable (stdio transport).

## How it works

1. On `initialize` (or lazily on first file open), the server finds the Git root of the
   workspace (walks up looking for `.git`; falls back to the workspace folder) and
   opens/creates an ontoenv environment there (`.ontoenv/` directory). Opening any file
   automatically loads the environment and triggers validation — no manual setup needed.
2. On document open/change (debounced ~0.8s) and save:
   - The document is parsed with rdflib (parse errors become diagnostics, positioned at
     the line reported by the parser).
   - The file is registered with ontoenv, which downloads any missing `owl:imports`.
     Imports that cannot be fetched (404, network error, etc.) are published as **error
     diagnostics** on the `owl:imports` line, with the underlying error message.
   - The transitive import closure is materialized as the shapes graph.
   - `shifty.validate(data, shapes, infer=True)` runs SHACL-AF rules to a fixed point and
     validates; results are mapped to diagnostics, positioned at the line where the focus
     node IRI (or its local name) first appears in the file.
3. On save, ontoenv re-scans the repo so newly added `.ttl` files join the environment.

## Commands

Executable via `workspace/executeCommand` from your editor:

- `shifty-lsp.refreshEnvironment` — re-scan the repository and rebuild the ontoenv
  environment, then revalidate all open documents.
- `shifty-lsp.validateWorkspace` — validate every `.ttl` file in the repository (open or
  not) and publish diagnostics for each.

In Neovim: `:lua vim.lsp.buf.execute_command({ command = "shifty-lsp.validateWorkspace" })`.

## Editor setup

### Neovim (nvim-lspconfig style)

```lua
vim.api.nvim_create_autocmd("FileType", {
  pattern = { "turtle", "ttl" },
  callback = function()
    vim.lsp.start({
      name = "shifty-lsp",
      cmd = { "/path/to/shifty-lsp/.venv/bin/shifty-lsp" },
      root_dir = vim.fs.root(0, ".git") or vim.fn.getcwd(),
    })
  end,
})
```

### VS Code

Use a generic LSP client extension (e.g. *Generic LSP Client*) and point it at the
`shifty-lsp` executable for `.ttl` files.

## Development

```sh
uv venv && uv pip install -e . 
.venv/bin/python test_e2e.py   # end-to-end LSP smoke test
```
