-- Neovim >= 0.11 LSP config for shifty-lsp.
-- After installing this plugin, just: vim.lsp.enable('shifty-lsp')
return {
  cmd = { 'shifty-lsp' },
  filetypes = { 'turtle' },
  root_markers = { '.git' },
}
