// Exercise the OpenCode gate plugin with a fake Bun Shell and print JSON decisions.
// Usage: node --no-warnings opencode_harness.mjs <plugin.js> <repoRoot> <sessionDir> <homeDir> <python>
import { spawnSync } from "node:child_process";
import { relative } from "node:path";
import { pathToFileURL } from "node:url";

const [pluginPath, repoRoot, sessionDir, homeDir, pythonExe] = process.argv.slice(2);

const { SyncintoshGates } = await import(pathToFileURL(pluginPath).href);

// A minimal stand-in for Bun Shell: it keeps the template's script path and Response
// payload, honors `.cwd()`, and actually runs the real hook so the decision is genuine.
function makeDollar() {
  const dollar = (_strings, ...values) => {
    const script = String(values[0]);
    const response = values.find((value) => value instanceof Response);
    let cwd;
    const chain = {
      cwd(dir) {
        cwd = dir;
        return chain;
      },
      quiet() {
        return chain;
      },
      nothrow() {
        return chain;
      },
      then(resolve, reject) {
        (async () => {
          const stdin = response ? await response.text() : "";
          const done = spawnSync(pythonExe, [script, "--tool", "opencode"], {
            input: stdin,
            encoding: "utf8",
            cwd,
            env: { ...process.env, HOME: homeDir },
          });
          resolve({ exitCode: done.status ?? 1, stderr: done.stderr || "" });
        })().then(resolve, reject);
      },
    };
    return chain;
  };
  return dollar;
}

const handlers = await SyncintoshGates({ $: makeDollar(), directory: sessionDir, worktree: repoRoot });

const before = async (tool, args) => {
  try {
    await handlers["tool.execute.before"]({ tool }, { args });
    return { blocked: false };
  } catch {
    return { blocked: true };
  }
};

const relToSecret = relative(sessionDir, `${homeDir}/.ssh/known_hosts`);
const patchKeyPem = "*** Begin Patch\n*** Add File: /tmp/leak/key.pem\n*** End Patch";

console.log(
  JSON.stringify({
    bashRelativeSecret: await before("bash", { command: `grep -r x ${relToSecret}` }),
    patchKeyPem: await before("apply_patch", { patchText: patchKeyPem }),
    bashBenign: await before("bash", { command: "git status" }),
    // Path-argument tool: OpenCode's `read` delivers the file in `filePath`.
    readSecret: await before("read", { filePath: `${homeDir}/.ssh/known_hosts` }),
  }),
);
