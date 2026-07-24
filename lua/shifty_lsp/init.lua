--- shifty-lsp Neovim plugin.
---
--- Usage:
---   require('shifty_lsp').setup()
---   vim.lsp.enable('shifty-lsp')
---
--- setup() wires buffer-local keymaps, user commands, and sane diagnostic
--- display whenever a shifty-lsp client attaches. All features can be turned
--- off individually via the opts table.

local M = {}

local defaults = {
  -- set buffer-local keymaps (gd, K, gr, <leader>ca, [e/]e, [E/]E)
  keymaps = true,
  -- create :ShiftyValidate and :ShiftyRefreshEnv buffer commands
  commands = true,
  -- configure vim.diagnostic with virtual_lines for shifty-lsp buffers
  diagnostics = true,
}

---@param opts? table
function M.setup(opts)
  opts = vim.tbl_deep_extend('force', defaults, opts or {})

  if opts.diagnostics then
    vim.diagnostic.config({
      signs = true,
      underline = true,
      virtual_lines = true,
    })
  end

  vim.api.nvim_create_autocmd('LspAttach', {
    group = vim.api.nvim_create_augroup('ShiftyLsp', { clear = true }),
    callback = function(args)
      local client = vim.lsp.get_client_by_id(args.data.client_id)
      if not client or client.name ~= 'shifty-lsp' then
        return
      end
      local bufnr = args.buf

      if opts.keymaps then
        local map = function(lhs, rhs, desc)
          vim.keymap.set('n', lhs, rhs, { buffer = bufnr, desc = desc })
        end
        map('gd', vim.lsp.buf.definition, 'goto definition')
        map('K', vim.lsp.buf.hover, 'hover docs')
        map('gr', vim.lsp.buf.references, 'references')
        map('<leader>ca', vim.lsp.buf.code_action, 'code action')
        map(']e', function()
          vim.diagnostic.jump({ count = 1, float = true })
        end, 'next diagnostic')
        map('[e', function()
          vim.diagnostic.jump({ count = -1, float = true })
        end, 'prev diagnostic')
        map(']E', function()
          vim.diagnostic.jump({ count = 1, float = true, severity = vim.diagnostic.severity.ERROR })
        end, 'next error')
        map('[E', function()
          vim.diagnostic.jump({ count = -1, float = true, severity = vim.diagnostic.severity.ERROR })
        end, 'prev error')
      end

      if opts.commands then
        vim.api.nvim_buf_create_user_command(bufnr, 'ShiftyValidate', function()
          client:request('workspace/executeCommand', {
            command = 'shifty-lsp.validateFile',
            arguments = { vim.uri_from_bufnr(bufnr) },
          }, function(err, result)
            if err then
              vim.notify('shifty-lsp error: ' .. tostring(err.message), vim.log.levels.ERROR)
              return
            end
            vim.notify(
              result and '\226\156\147 conforms' or '\226\156\151 violations found',
              result and vim.log.levels.INFO or vim.log.levels.ERROR
            )
          end, bufnr)
        end, { desc = 'shifty: validate this file' })

        vim.api.nvim_buf_create_user_command(bufnr, 'ShiftyRefreshEnv', function()
          client:request('workspace/executeCommand', {
            command = 'shifty-lsp.refreshEnvironment',
          }, function()
            vim.notify('ontoenv environment refreshed')
          end, bufnr)
        end, { desc = 'ontoenv: refresh environment' })
      end
    end,
  })
end

return M
