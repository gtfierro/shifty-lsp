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
- `shifty-lsp.validateFile` — validate a single file (pass the document URI as the first
  argument; defaults to the current document) and publish diagnostics. Returns `true` if
  the file conforms, `false` otherwise.

## Goto definition

`textDocument/definition` is supported: with the cursor on an ontology IRI or prefixed
name (e.g. inside an `owl:imports <...>` statement), jump to that ontology's `.ttl` file.
Local ontologies open directly; remote ones are materialized from the ontoenv store into
`.ontoenv/lsp-cache/<name>.ttl` on first use and opened from there.

## Editor setup

### Neovim (nvim-lspconfig style)

```lua
vim.api.nvim_create_autocmd("FileType", {
  pattern = { "turtle", "ttl" },
  callback = function(args)
    vim.lsp.start({
      name = "shifty-lsp",
      cmd = { "/path/to/shifty-lsp/.venv/bin/shifty-lsp" },
      root_dir = vim.fs.root(0, ".git") or vim.fn.getcwd(),
    })

    -- inline diagnostics
    vim.diagnostic.config({ virtual_text = true, signs = true, underline = true })

    -- goto definition (jump to an ontology's .ttl from its IRI in owl:imports)
    vim.keymap.set("n", "gd", vim.lsp.buf.definition, { buffer = args.buf, desc = "goto definition" })

    -- validate the current file, showing the conforms result
    vim.api.nvim_buf_create_user_command(args.buf, "ShiftyValidate", function()
      local res = vim.lsp.buf_request_sync(0, "workspace/executeCommand", {
        command = "shifty-lsp.validateFile",
        arguments = { vim.uri_from_bufnr(0) },
      }, 60000)
      local conforms = res and next(res) and vim.iter(vim.tbl_values(res)):all(function(r) return r.result end)
      vim.notify(conforms and "\226\156\147 conforms" or "\226\156\151 violations found",
        conforms and vim.log.levels.INFO or vim.log.levels.ERROR)
    end, {})

    -- refresh the ontoenv environment
    vim.api.nvim_buf_create_user_command(args.buf, "ShiftyRefreshEnv", function()
      vim.lsp.buf.execute_command({ command = "shifty-lsp.refreshEnvironment" })
    end, {})

    vim.keymap.set("n", "<leader>ov", "<cmd>ShiftyValidate<cr>", { buffer = args.buf, desc = "shifty: validate file" })
    vim.keymap.set("n", "<leader>or", "<cmd>ShiftyRefreshEnv<cr>", { buffer = args.buf, desc = "ontoenv: refresh environment" })
  end,
})
```

Progress during validation (import downloads, shifty runs) is reported via
`window/workDoneProgress` — install [fidget.nvim](https://github.com/j-hui/fidget.nvim)
to see it as a status widget.

### VS Code

Use a generic LSP client extension (e.g. *Generic LSP Client*) and point it at the
`shifty-lsp` executable for `.ttl` files.

## Development

```sh
uv venv && uv pip install -e . 
.venv/bin/python test_e2e.py   # end-to-end LSP smoke test
```
