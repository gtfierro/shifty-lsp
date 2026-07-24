const vscode = require("vscode");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");

/** @type {LanguageClient | undefined} */
let client;

function activate(context) {
  const config = vscode.workspace.getConfiguration("shifty-lsp");
  const command = config.get("path", "shifty-lsp");

  client = new LanguageClient(
    "shifty-lsp",
    "shifty-lsp",
    { command, transport: TransportKind.stdio },
    {
      documentSelector: [{ scheme: "file", language: "turtle" }],
      // The server roots its ontoenv environment at the git repository.
      workspaceFolder: vscode.workspace.workspaceFolders?.[0],
    }
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("shifty-lsp.validateFile", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const result = await client.sendRequest("workspace/executeCommand", {
        command: "shifty-lsp.validateFile",
        arguments: [editor.document.uri.toString()],
      });
      if (result) {
        vscode.window.showInformationMessage("shifty: file conforms ✓");
      } else {
        vscode.window.showWarningMessage(
          "shifty: violations found — see Problems panel"
        );
      }
    }),
    vscode.commands.registerCommand("shifty-lsp.refreshEnvironment", async () => {
      await client.sendRequest("workspace/executeCommand", {
        command: "shifty-lsp.refreshEnvironment",
        arguments: [],
      });
      vscode.window.showInformationMessage("shifty: ontoenv environment refreshed");
    })
  );

  client.start();
}

function deactivate() {
  return client ? client.stop() : undefined;
}

module.exports = { activate, deactivate };
