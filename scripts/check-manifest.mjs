#!/usr/bin/env node
/**
 * manifest check: the map from channel name to directory is complete enough to
 * publish. Three assertions, the ones CI's manifest lane runs:
 *
 *   dirs       every `channels.<name>.dir` exists and is a directory
 *   coverage   package.json `files` ships that dir — a channel left out of the
 *              tarball registers from a checkout and fails for every operator
 *   contracts  a `drain` and a `deliver` range are declared; core's register
 *              refuses a manifest without them, so this names the cause early
 *
 * Per-file lists are not the manifest's business: core records the hashes when
 * it syncs the channel, so this check reads no file bytes.
 *
 * Exit: 0 every mapped dir is present and publishable, 1 an assertion failed,
 * 2 the check could not run at all.
 */
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const PLANES = ["drain", "deliver"];

function readJson(name) {
  return JSON.parse(readFileSync(join(repoRoot, name), "utf8"));
}

function main() {
  let manifest, packageJson;
  try {
    manifest = readJson("manifest.json");
    packageJson = readJson("package.json");
  } catch (error) {
    console.log(`error: no_manifest: ${error.message}; run this from a checkout of agents-pod-channels`);
    return 2;
  }

  const channels = Object.entries(manifest.channels ?? {});
  if (channels.length === 0) {
    console.log("error: no_channels: manifest.json maps no channel; a package with no resident channel ships nothing");
    return 1;
  }

  const shipped = (packageJson.files ?? []).map((entry) => entry.replace(/\/+$/, ""));
  const covers = (dir) => shipped.some((entry) => dir === entry || dir.startsWith(`${entry}/`));
  const failures = [];

  for (const [name, channel] of channels) {
    const dir = channel?.dir;
    if (typeof dir !== "string" || dir === "") {
      failures.push(`${name}: no dir — manifest.json needs {"channels":{"${name}":{"dir":"channels/${name}"}}}`);
      continue;
    }
    const path = join(repoRoot, dir);
    if (!existsSync(path) || !statSync(path).isDirectory()) {
      failures.push(`${name}: ${dir} is not a directory — the map points at bytes that are not there`);
      continue;
    }
    if (!covers(dir)) {
      failures.push(`${name}: ${dir} is not shipped — add "channels/" to package.json files`);
      continue;
    }
    const files = readdirSync(path).filter((entry) => statSync(join(path, entry)).isFile()).sort();
    console.log(`ok    ${name.padEnd(12)} ${dir.padEnd(24)} ${files.length} file(s)`);
  }

  const undeclared = PLANES.filter((plane) => typeof manifest.contracts?.[plane] !== "string");
  for (const plane of undeclared) {
    failures.push(`contracts: no ${plane} range — core's register refuses a manifest without contracts`);
  }

  for (const failure of failures) console.log(`error: manifest: ${failure}`);
  if (failures.length > 0) {
    console.log("next: fix the error lines, then `node scripts/check-manifest.mjs` again");
    return 1;
  }
  console.log(`manifest: ${channels.length} channel(s), ${PLANES.join(" + ")} contracts declared`);
  return 0;
}

process.exitCode = main();
