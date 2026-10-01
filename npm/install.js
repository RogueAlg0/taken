"use strict";
// Postinstall for taken-gh: make sure the real `taken` Python CLI is
// available, since the bin shims forward to it. Never fails the npm install:
// every error path prints guidance and exits 0.
const { spawnSync } = require("node:child_process");
const pkg = require("./package.json");
const VERSION = pkg.version;

function manualInstallHint() {
  return (
    "You can install it by hand with:\n" +
    `  python3 -m pip install --user "taken-gh==${VERSION}"`
  );
}

function main() {
  try {
    const existing = spawnSync("taken", ["--version"], { encoding: "utf8" });
    if (existing.status === 0) {
      console.log("taken is already installed, skipping the pip install.");
      return;
    }

    console.log(`Installing taken-gh ${VERSION} with pip...`);
    const result = spawnSync(
      "python3",
      ["-m", "pip", "install", "--user", `taken-gh==${VERSION}`],
      { stdio: "inherit" }
    );

    if (result.error && result.error.code === "ENOENT") {
      console.error(
        "python3 was not found on PATH, so the pip install was skipped.\n" +
          manualInstallHint()
      );
      return;
    }

    if (result.status !== 0) {
      console.error(
        "The pip install did not finish cleanly, so the taken command may not work yet.\n" +
          manualInstallHint()
      );
      return;
    }

    console.log("taken installed. Run `taken --help` to get started.");
  } catch (err) {
    console.error(`Postinstall hit a snag and skipped the pip install: ${err.message}`);
    console.error(manualInstallHint());
  }
  process.exit(0);
}

main();
