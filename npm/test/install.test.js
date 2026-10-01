"use strict";
// Tests for install.js: skip pip when taken already works, pip-install when
// it does not, and never fail the npm install no matter what goes wrong.
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const INSTALL = path.join(__dirname, "..", "install.js");
const PKG = require("../package.json");

function makeTempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "taken-install-"));
}

function writeExecutable(dir, name, body) {
  const p = path.join(dir, name);
  fs.writeFileSync(p, body);
  fs.chmodSync(p, 0o755);
  return p;
}

function runInstall(env) {
  return spawnSync(process.execPath, [INSTALL], {
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
}

describe("install.js", () => {
  it("skips pip when `taken --version` already works", () => {
    const dir = makeTempDir();
    writeExecutable(
      dir,
      "taken",
      '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "taken 0.7.4"; exit 0; fi\nexit 1\n'
    );
    // A python3 that must never be called: it would fail loudly if invoked.
    writeExecutable(dir, "python3", '#!/bin/sh\necho "pip should not have run" >&2\nexit 99\n');
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    assert.match(r.stdout, /already installed, skipping/);
    assert.doesNotMatch(r.stdout + r.stderr, /pip should not have run/);
  });

  it("pip-installs the pinned version when taken is missing", () => {
    const dir = makeTempDir();
    const marker = path.join(dir, "pip-args.txt");
    writeExecutable(
      dir,
      "python3",
      `#!/bin/sh\necho "$*" > "${marker}"\nexit 0\n`
    );
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    const args = fs.readFileSync(marker, "utf8").trim();
    assert.equal(args, `-m pip install --user taken-gh==${PKG.version}`);
  });

  it("exits 0 with guidance when python3 is missing entirely", () => {
    const dir = makeTempDir(); // empty: no taken, no python3
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    assert.match(r.stdout + r.stderr, /python3 was not found/);
    assert.match(r.stdout + r.stderr, new RegExp(`taken-gh==${PKG.version}`));
  });

  it("exits 0 with guidance when pip itself fails", () => {
    const dir = makeTempDir();
    writeExecutable(dir, "python3", "#!/bin/sh\nexit 1\n");
    const r = runInstall({ PATH: dir });
    assert.equal(r.status, 0);
    assert.match(r.stdout + r.stderr, /did not finish cleanly/);
  });
});
