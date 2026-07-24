# shifty-lsp

A language server for Turtle/RDF ontology files, combining:

- **[ontoenv](https://ontoenv.gtf.fyi)** — manages an ontology environment rooted at the
  enclosing Git repository. All `*.ttl` files in the repo are discovered automatically, and
  `owl:imports` declarations are resolved and downloaded into a local cache.
- **[shifty](https://shifty.gtf.fyi)** — continuous SHACL validation + SHACL-AF inference.
  The document is validated against the transitive closure of its `owl:imports` (used as the
  shapes graph). `sh:Violation`, `sh:Warning`, and `sh:Info` results are published as LSP
  error/warning/information diagnostics.

## Features

| Feature | LSP method |
|---|---|
| Continuous SHACL validation + inference, results as diagnostics | `textDocument/publishDiagnostics` |
| Turtle syntax errors as diagnostics (correct line) | 〃 |
| Unresolvable `owl:imports` (404 etc.) as error diagnostics | 〃 |
| Prefix-aware completion from the ontology environment (`brick:IAQ…`) | `textDocument/completion` |
| Hover docs (`rdfs:label`/`comment`/types/supertypes) | `textDocument/hover` |
| Goto definition of ontology IRIs (remote ontologies materialized locally) | `textDocument/definition` |
| References, repo-wide | `textDocument/references` |
| Document outline (ontology, classes, shapes, individuals) | `textDocument/documentSymbol` |
| Quickfix: declare missing prefix (+ `owl:imports`) | `textDocument/codeAction` |
| Progress reporting during downloads/validation | `window/workDoneProgress` |
| Validation summary messages | `window/showMessage` |

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
2. On document open/change (debounced ~0.8s, in a background thread) and save:
   - The document is parsed with rdflib (parse errors become diagnostics, positioned at
     the line reported by the parser).
   - The file is registered with ontoenv, which downloads any missing `owl:imports`.
     Imports that cannot be fetched (404, network error, etc.) are published as **error
     diagnostics** on the `owl:imports` line, with the underlying error message.
   - The transitive import closure is materialized as the shapes graph. It is **cached
     per document** and only rebuilt when the file's `owl:imports` set changes or the
     `refreshEnvironment` command runs.
   - `shifty.validate(data, shapes, infer=True)` runs SHACL-AF rules to a fixed point and
     validates. Each result is mapped to diagnostics at **every line where the offending
     node appears**: the `sh:value` node when the report identifies one, otherwise the
     `sh:focusNode`. Matching is prefix-aware (`ex:Foo`, `<full-iri>`, local name) with
     token boundaries.
   - If you keep typing while a slow validation is in flight, the stale run's results
     are discarded rather than overwriting fresher state.
   - When validation completes, a `window/showMessage` summary is sent
     (`shifty: file.ttl: conforms ✓` / `N error(s), M warning(s)`).
3. On save, ontoenv re-scans the repo so newly added `.ttl` files join the environment
   (this does not force shapes-graph rebuilds — see `refreshEnvironment` for that).

## Commands

Executable via `workspace/executeCommand` from your editor:

- `shifty-lsp.refreshEnvironment` — re-scan the repository, invalidate cached shapes
  graphs, and revalidate all open documents. Use this after editing an ontology that
  *other* files import (the per-document import-set cache can't detect that case).
- `shifty-lsp.validateFile` — validate a single file (pass the document URI as the first
  argument; defaults to the current document) and publish diagnostics. Returns `true` if
  the file conforms, `false` otherwise.

## Completion

`textDocument/completion` (triggered by `:`): typing a prefixed name like `brick:IAQ`
offers every class, property, and individual in that namespace, drawn from the merged
imports closure in the ontoenv environment. Items carry the term's `rdfs:label` as detail
and `rdfs:comment` as documentation. Completion works even while the buffer doesn't parse
(imports are recovered with a regex), and the client filters as you keep typing.

## Hover

`textDocument/hover`: cursor on any IRI or prefixed name shows a markdown card with the
term's `rdfs:label`, `rdf:type`, `rdfs:subClassOf` chain, and `rdfs:comment`, pulled from
the merged ontology environment.

## References

`textDocument/references`: finds every occurrence of the term under the cursor, both in
the current file and in all other `.ttl` files in the repository (`.ontoenv/` excluded).

## Code actions (quickfix)

`textDocument/codeAction`: when a prefixed name is used whose prefix is not declared in
the file but **is** a known namespace in the ontology environment, offers a quickfix that
inserts the `@prefix` declaration — plus an `owl:imports` statement when the prefix maps
to a known ontology. Works even while the buffer doesn't parse (that's usually why the
prefix is missing).

## Document symbols (outline)

`textDocument/documentSymbol` returns the ontology declaration plus every typed subject
in the file (classes, properties, SHACL shapes, individuals) with their `rdf:type` as
detail. Use it via `gO` (built-in location list), `:Telescope lsp_document_symbols`, or
outline plugins like aerial.nvim.

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

    -- inline diagnostics: virtual_lines renders each SHACL message on its own
    -- line(s) below the offending code -- better for long validation messages.
    -- Neovim wraps them to the window width automatically.
    vim.diagnostic.config({
      signs = true,
      underline = true,
      virtual_lines = true,
      -- alternative: end-of-line virtual text, wrapped to the window width.
      -- Requires a wrap_text(msg, width) helper that soft-wraps on word
      -- boundaries and joins with "\n".
      -- virtual_text = {
      --   format = function(d)
      --     local width = vim.api.nvim_win_get_width(0) - d.col - 8
      --     return wrap_text(d.message, math.max(width, 40))
      --   end,
      -- },
    })

    -- completion: manual via omnifunc (<C-x><C-o>), or wire up nvim-cmp/blink.cmp
    -- with the 'nvim_lsp' source for as-you-type completion
    vim.bo[args.buf].omnifunc = "v:lua.vim.lsp.omnifunc"

    -- goto definition (jump to an ontology's .ttl from its IRI in owl:imports)
    vim.keymap.set("n", "gd", vim.lsp.buf.definition, { buffer = args.buf, desc = "goto definition" })

    -- hover: label/comment/types for the term under the cursor
    vim.keymap.set("n", "K", vim.lsp.buf.hover, { buffer = args.buf, desc = "hover docs" })

    -- references: every use of the term (this file + repo .ttl files)
    vim.keymap.set("n", "gr", vim.lsp.buf.references, { buffer = args.buf, desc = "references" })

    -- code actions: e.g. "Declare prefix 'sh:' and import ..." quickfix
    vim.keymap.set("n", "<leader>ca", vim.lsp.buf.code_action, { buffer = args.buf, desc = "code action" })

    -- jump between diagnostics from the cursor position (float shows the message)
    vim.keymap.set("n", "]e", function()
      vim.diagnostic.jump({ count = 1, float = true })
    end, { buffer = args.buf, desc = "next diagnostic" })
    vim.keymap.set("n", "[e", function()
      vim.diagnostic.jump({ count = -1, float = true })
    end, { buffer = args.buf, desc = "prev diagnostic" })
    -- errors only (skip warnings/infos):
    vim.keymap.set("n", "]E", function()
      vim.diagnostic.jump({ count = 1, float = true, severity = vim.diagnostic.severity.ERROR })
    end, { buffer = args.buf, desc = "next error" })
    vim.keymap.set("n", "[E", function()
      vim.diagnostic.jump({ count = -1, float = true, severity = vim.diagnostic.severity.ERROR })
    end, { buffer = args.buf, desc = "prev error" })

    -- validate the current file, showing the conforms result.
    -- NOTE: async request -- a synchronous buf_request_sync would block the
    -- UI and swallow the server's progress notifications.
    vim.api.nvim_buf_create_user_command(args.buf, "ShiftyValidate", function()
      local client = vim.lsp.get_clients({ bufnr = 0, name = "shifty-lsp" })[1]
      if not client then return end
      client:request("workspace/executeCommand", {
        command = "shifty-lsp.validateFile",
        arguments = { vim.uri_from_bufnr(0) },
      }, function(err, result)
        if err then
          vim.notify("shifty-lsp error: " .. tostring(err.message), vim.log.levels.ERROR)
          return
        end
        vim.notify(result and "\226\156\147 conforms" or "\226\156\151 violations found",
          result and vim.log.levels.INFO or vim.log.levels.ERROR)
      end, 0)
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
`window/workDoneProgress`. Stock Neovim does **not** display this anywhere visible —
either install [fidget.nvim](https://github.com/j-hui/fidget.nvim) (`opts = {}` is
enough), or add this autocmd to echo progress to the message area:

```lua
vim.api.nvim_create_autocmd("LspProgress", {
  callback = function(ev)
    local value = ev.data.params.value
    if value.kind == "end" then return end
    local msg = value.title or ""
    if value.message then msg = msg .. ": " .. value.message end
    vim.api.nvim_echo({ { msg, "Comment" } }, false, {})
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
