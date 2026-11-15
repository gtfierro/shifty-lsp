-- shifty-lsp plugin bootstrap: filetype detection for Turtle files.
if vim.g.loaded_shifty_lsp then
  return
end
vim.g.loaded_shifty_lsp = 1

-- Keep in sync with DEFAULT_SOURCE_SUFFIXES in shifty_lsp/server.py.
vim.filetype.add({
  extension = {
    ttl = 'turtle',
  },
  pattern = {
    -- literate Turtle; `extension` only sees the last suffix ("md")
    ['.*%.ttl%.md'] = 'turtle',
  },
})
