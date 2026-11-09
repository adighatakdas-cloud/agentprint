#!/usr/bin/env node
// npm wrapper for agentprint: runs the Python agentprint CLI of the same version through
// `uvx` (preferred) or `pipx run`. No dependencies, no install scripts, no telemetry.
'use strict';

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const INSTALL_HINT =
  'agentprint: needs uv or pipx to run. Install uv (https://docs.astral.sh/uv/), then retry.';

const PRE = { alpha: 'a', beta: 'b', rc: 'rc' };

// npm version (semver) -> Python version (PEP 440): 1.2.3-rc.1 -> 1.2.3rc1, 0.1.0-dev.0 -> 0.1.0.dev0
function pythonVersion(version) {
  const m = /^(\d+\.\d+\.\d+)(?:-(alpha|beta|rc|dev)\.(\d+))?$/.exec(version);
  if (!m) throw new Error(`unsupported version: ${version}`);
  const [, base, tag, n] = m;
  if (!tag) return base;
  return tag === 'dev' ? `${base}.dev${n}` : `${base}${PRE[tag]}${n}`;
}

// Full path of an executable on PATH, or null. Never runs anything. Folders inside
// node_modules are skipped: npm puts node_modules/.bin first on PATH for package scripts
// (prepublishOnly), and a dependency must not be able to stand in for uvx or pipx.
function findExecutable(name, env = process.env) {
  const windows = process.platform === 'win32';
  const dirs = (env.PATH || env.Path || '')
    .split(path.delimiter)
    .filter((dir) => dir && !dir.split(/[\\/]/).includes('node_modules'));
  for (const dir of dirs) {
    const file = path.join(dir, windows ? `${name}.exe` : name);
    try {
      if (!fs.statSync(file).isFile()) continue;
      if (!windows) fs.accessSync(file, fs.constants.X_OK);
      return file;
    } catch {
      // missing or not executable: keep looking
    }
  }
  return null;
}

// The program and arguments to run, or null when neither uvx nor pipx is installed.
function command(args, version, which = findExecutable) {
  const spec = `agentprint==${pythonVersion(version)}`;
  const uvx = which('uvx');
  if (uvx) return { file: uvx, args: [spec, ...args] };
  const pipx = which('pipx');
  if (pipx) return { file: pipx, args: ['run', '--spec', spec, 'agentprint', ...args] };
  return null;
}

// `--if-unavailable allow` (what `agentprint protect` puts in a shared prepublishOnly): when
// agentprint can't run here (no uv or pipx, or it fails to start), say so in one line and exit 0,
// so a teammate or CI without uv is not blocked. Exit 1 (something would leak) still blocks.
function allowsUnavailable(args) {
  const at = args.indexOf('--if-unavailable');
  return args.includes('--if-unavailable=allow') || (at !== -1 && args[at + 1] === 'allow');
}

function unavailable(why, allow) {
  if (!allow) return 2;
  process.stderr.write(`agentprint: ${why}; the publish goes on (--if-unavailable allow)\n`);
  return 0;
}

function main() {
  const { version } = require('../package.json');
  const args = process.argv.slice(2);
  const allow = allowsUnavailable(args);
  const cmd = command(args, version);
  if (cmd === null) {
    if (!allow) process.stderr.write(`${INSTALL_HINT}\n`);
    process.exit(unavailable('could not run: uv or pipx is not installed', allow));
  }
  // No shell: arguments reach agentprint exactly as given. stdio is shared, so hook payloads on
  // stdin and JSON/SARIF on stdout pass straight through.
  const child = spawn(cmd.file, cmd.args, { stdio: 'inherit' });
  // Ctrl-C already reaches the child (same process group); other signals are forwarded.
  process.on('SIGINT', () => {});
  for (const signal of ['SIGTERM', 'SIGHUP']) process.on(signal, () => child.kill(signal));
  child.on('error', (err) => {
    const why = `could not start ${path.basename(cmd.file)}: ${err.message}`;
    if (!allow) process.stderr.write(`agentprint: ${why}\n`);
    process.exit(unavailable(why, allow));
  });
  child.on('exit', (code, signal) => {
    if (signal) process.exit(128 + (os.constants.signals[signal] || 0));
    if (allow && code !== 0 && code !== 1) {
      process.exit(unavailable(`could not run (exit ${code})`, allow));
    }
    process.exit(code ?? 2);
  });
}

if (require.main === module) main();

module.exports = { allowsUnavailable, command, findExecutable, pythonVersion };
