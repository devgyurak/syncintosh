// Exercise the Pi gate extension with a fake `pi` object and print JSON decisions.
// Usage: node --no-warnings pi_harness.mjs <extension.ts> <sessionDir> <homeDir>
import { relative } from "node:path";
import { pathToFileURL } from "node:url";

const [extensionPath, sessionDir, homeDir] = process.argv.slice(2);

// The extension lives outside a package.json `type: module` scope; type-only imports are
// stripped by Node, so it imports cleanly on Node >= 23.
const { default: factory } = await import(pathToFileURL(extensionPath).href);

// SECRET_DIRS in the hook is derived from HOME, so point it at the temp home.
process.env.HOME = homeDir;

const handlers = {};
const pi = { on: (name, fn) => (handlers[name] = fn) };
factory(pi);

const ctx = { cwd: sessionDir, ui: { notify() {} } };
const runBash = (command) => handlers.tool_call({ toolName: "bash", input: { command } }, ctx);

// A relative path into a SECRET_DIR whose basename is not itself blocked, so the only thing
// that blocks it is resolving the path against the session cwd.
const relToSecret = relative(sessionDir, `${homeDir}/.ssh/known_hosts`);

console.log(
  JSON.stringify({
    bashEnv: (await runBash("cat .env")) ?? null,
    // `grep` (not a file-read verb) so only token resolution decides — which depends on cwd.
    bashRelativeSecret: (await runBash(`grep -r x ${relToSecret}`)) ?? null,
    bashBenign: (await runBash("git status")) ?? null,
    // Path-argument tools: the most common way an agent reads a secret file.
    readSecret:
      (await handlers.tool_call({ toolName: "read", input: { path: `${homeDir}/.ssh/known_hosts` } }, ctx)) ??
      null,
    grepSecret:
      (await handlers.tool_call({ toolName: "grep", input: { pattern: "x", path: `${homeDir}/.ssh` } }, ctx)) ??
      null,
  }),
);
