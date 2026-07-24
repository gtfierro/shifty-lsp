-- shifty-lsp plugin bootstrap: filetype detection for Turtle files.
if vim.g.loaded_shifty_lsp then
  return
end
vim.g.loaded_shifty_lsp = 1

vim.filetype.add({
  extension = {
    ttl = 'turtle',
  },
})
