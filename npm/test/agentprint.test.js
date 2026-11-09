'use strict';

const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  allowsUnavailable,
  command,
  findExecutable,
  pythonVersion,
} = require('../bin/agentprint.js');
const pkg = require('../package.json');

const BIN = path.join(__dirname, '..', 'bin', 'agentprint.js');
const INIT = path.join(__dirname, '..', '..', 'agentprint', '__init__.py');
const unixOnly = { skip: process.platform === 'win32' && 'fake runners are shell scripts' };

// A fake runner prints its name and each argument on its own line, writes to stderr, and exits
// with $FAKE_EXIT. With $FAKE_CAT set it echoes stdin; with $FAKE_SIGNAL it kills itself.
const FAKE = `#!/bin/sh
if [ -n "$FAKE_SIGNAL" ]; then kill -"$FAKE_SIGNAL" $$; fi
if [ -n "$FAKE_CAT" ]; then /bin/cat; exit 0; fi
printf '%s\\n' "\${0##*/}"
for arg in "$@"; do printf '%s\\n' "$arg"; done
printf 'to-stderr\\n' >&2
exit "\${FAKE_EXIT:-0}"
`;

const made = [];
test.after(() => {
  for (const dir of made) fs.rmSync(dir, { recursive: true, force: true });
});

function fakeDir(...names) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agentprint-npm-'));
  made.push(dir);
  for (const name of names) fs.writeFileSync(path.join(dir, name), FAKE, { mode: 0o755 });
  return dir;
}

function run(dir, args, env = {}, input) {
  return spawnSync(process.execPath, [BIN, ...args], {
    env: { PATH: dir, ...env },
    input,
    encoding: 'utf8',
  });
}

const SPEC = `agentprint==${pythonVersion(pkg.version)}`;

test('npm versions map to Python versions', () => {
  assert.equal(pythonVersion('1.2.3'), '1.2.3');
  assert.equal(pythonVersion('1.2.3-alpha.1'), '1.2.3a1');
  assert.equal(pythonVersion('1.2.3-beta.2'), '1.2.3b2');
  assert.equal(pythonVersion('1.2.3-rc.3'), '1.2.3rc3');
  assert.equal(pythonVersion('0.1.0-dev.0'), '0.1.0.dev0');
  assert.throws(() => pythonVersion('1.2'));
  assert.throws(() => pythonVersion('1.2.3-next.1'));
});

test('package version matches agentprint/__init__.py', { skip: !fs.existsSync(INIT) }, () => {
  const [, version] = /__version__ = "([^"]+)"/.exec(fs.readFileSync(INIT, 'utf8'));
  assert.equal(pythonVersion(pkg.version), version);
});

test('package ships only the bin script and runs no install scripts', () => {
  assert.deepEqual(pkg.bin, { agentprint: 'bin/agentprint.js' });
  assert.deepEqual(pkg.files, ['bin/agentprint.js']);
  assert.deepEqual(pkg.dependencies ?? {}, {});
  for (const hook of ['preinstall', 'install', 'postinstall', 'prepare']) {
    assert.equal(pkg.scripts?.[hook], undefined, hook);
  }
});

test('prefers uvx and pins the matching Python version', unixOnly, () => {
  const result = run(fakeDir('uvx', 'pipx'), ['scan', '--format', 'json']);
  assert.equal(result.status, 0);
  assert.deepEqual(result.stdout.trim().split('\n'), ['uvx', SPEC, 'scan', '--format', 'json']);
});

test('falls back to pipx run', unixOnly, () => {
  const result = run(fakeDir('pipx'), ['pack', '.']);
  assert.equal(result.status, 0);
  assert.deepEqual(result.stdout.trim().split('\n'), [
    'pipx',
    'run',
    '--spec',
    SPEC,
    'agentprint',
    'pack',
    '.',
  ]);
});

test('without uvx or pipx prints one install hint and exits 2', unixOnly, () => {
  const result = run(fakeDir(), ['scan']);
  assert.equal(result.status, 2);
  assert.equal(result.stdout, '');
  assert.equal(result.stderr.trim().split('\n').length, 1);
  assert.match(result.stderr, /uv/);
});

test('exit codes pass through', unixOnly, () => {
  const dir = fakeDir('uvx');
  for (const code of [0, 1, 2]) {
    assert.equal(run(dir, ['scan'], { FAKE_EXIT: String(code) }).status, code);
  }
});

test('a runner killed by a signal exits 128 + signal number', unixOnly, () => {
  const result = run(fakeDir('uvx'), ['scan'], { FAKE_SIGNAL: 'TERM' });
  assert.equal(result.status, 128 + os.constants.signals.SIGTERM);
});

test('stderr and stdin pass through', unixOnly, () => {
  const dir = fakeDir('uvx');
  assert.match(run(dir, ['scan']).stderr, /to-stderr/);
  const piped = run(dir, ['guard'], { FAKE_CAT: '1' }, '{"tool_input": {}}');
  assert.equal(piped.stdout, '{"tool_input": {}}');
});

test('arguments are passed as-is, never through a shell', unixOnly, () => {
  const tricky = ['a b', '$(touch pwned)', "'; echo x", '--format=sarif'];
  const dir = fakeDir('uvx');
  const result = run(dir, tricky);
  assert.deepEqual(result.stdout.trim().split('\n').slice(2), tricky);
  assert.equal(fs.existsSync(path.join(process.cwd(), 'pwned')), false);
});

test('findExecutable skips files that are not executable', unixOnly, () => {
  const dir = fakeDir();
  fs.writeFileSync(path.join(dir, 'uvx'), 'not a program', { mode: 0o644 });
  assert.equal(findExecutable('uvx', { PATH: dir }), null);
  fs.chmodSync(path.join(dir, 'uvx'), 0o755);
  assert.equal(findExecutable('uvx', { PATH: dir }), path.join(dir, 'uvx'));
});

test('command() returns null when nothing is installed', () => {
  assert.equal(
    command(['scan'], '1.0.0', () => null),
    null,
  );
  assert.deepEqual(command(['scan'], '1.0.0', (name) => `/bin/${name}`), {
    file: '/bin/uvx',
    args: ['agentprint==1.0.0', 'scan'],
  });
});

test('--if-unavailable allow lets the publish go when agentprint cannot run', unixOnly, () => {
  const allow = ['pack', '--fail-on', 'high', '--if-unavailable', 'allow'];
  const missing = run(fakeDir(), allow);
  assert.equal(missing.status, 0);
  assert.match(missing.stderr, /^agentprint: could not run: uv or pipx is not installed; the publish goes on/);
  const dir = fakeDir('uvx');
  assert.equal(run(dir, allow, { FAKE_EXIT: '2' }).status, 0); // uv or agentprint failed
  assert.equal(run(dir, allow, { FAKE_EXIT: '1' }).status, 1); // a leak still blocks
  assert.equal(run(dir, allow, { FAKE_EXIT: '0' }).status, 0);
  assert.equal(run(fakeDir(), ['pack']).status, 2); // without the flag: fail closed, as before
  assert.equal(run(dir, ['pack'], { FAKE_EXIT: '2' }).status, 2);
});

test('the flag is read in both spellings and passed on to agentprint', unixOnly, () => {
  assert.equal(allowsUnavailable(['pack', '--if-unavailable=allow']), true);
  assert.equal(allowsUnavailable(['pack', '--if-unavailable', 'block']), false);
  assert.equal(allowsUnavailable(['pack']), false);
  const result = run(fakeDir('uvx'), ['pack', '--if-unavailable', 'allow']);
  assert.deepEqual(result.stdout.trim().split('\n').slice(2), ['pack', '--if-unavailable', 'allow']);
});

test('uvx or pipx inside node_modules is never used', unixOnly, () => {
  const root = fakeDir();
  const bin = path.join(root, 'node_modules', '.bin');
  fs.mkdirSync(bin, { recursive: true });
  fs.writeFileSync(path.join(bin, 'uvx'), FAKE, { mode: 0o755 });
  assert.equal(findExecutable('uvx', { PATH: bin }), null);
  const real = fakeDir('uvx');
  assert.equal(findExecutable('uvx', { PATH: [bin, real].join(path.delimiter) }), path.join(real, 'uvx'));
});
