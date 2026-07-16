import crypto from "node:crypto";
import assert from "node:assert/strict";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(scriptDir, "..");

async function filesUnder(root, relative = "") {
  const directory = path.join(root, relative);
  const entries = await fsp.readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    const child = path.join(relative, entry.name);
    if (entry.isDirectory()) files.push(...await filesUnder(root, child));
    else if (entry.isFile()) files.push(child);
  }
  return files;
}

async function sameBytes(left, right) {
  const [a, b] = await Promise.all([fsp.readFile(left), fsp.readFile(right)]);
  return a.equals(b);
}

function referencedAssets(indexHtml) {
  const assets = new Set();
  for (const match of indexHtml.matchAll(/(?:src|href)=["']([^"']+)["']/g)) {
    const raw = match[1];
    if (/^(?:[a-z]+:)?\/\//i.test(raw)) throw new Error("dist_external_asset_not_allowed");
    const asset = raw.split(/[?#]/, 1)[0].replace(/^\//, "");
    if (!asset || asset === "index.html") continue;
    if (asset.split("/").some((part) => part === "..")) throw new Error("dist_asset_path_invalid");
    assets.add(asset);
  }
  return assets;
}

async function validateStage(stage) {
  const indexPath = path.join(stage, "index.html");
  const html = await fsp.readFile(indexPath, "utf8");
  for (const asset of referencedAssets(html)) {
    const stat = await fsp.stat(path.join(stage, asset));
    if (!stat.isFile()) throw new Error("dist_missing_asset");
  }
}

async function atomicCopy(source, destination) {
  await fsp.mkdir(path.dirname(destination), { recursive: true });
  const temporary = path.join(path.dirname(destination), `.${path.basename(destination)}.tmp-${crypto.randomUUID()}`);
  try {
    await fsp.copyFile(source, temporary, fs.constants.COPYFILE_EXCL);
    await fsp.rename(temporary, destination);
  } finally {
    await fsp.rm(temporary, { force: true }).catch(() => undefined);
  }
}

export async function publishStage(stage, dist, { failBeforeIndex = false } = {}) {
  await validateStage(stage);
  await fsp.mkdir(dist, { recursive: true });
  const oldFiles = new Set(await filesUnder(dist).catch(() => []));
  const newFiles = await filesUnder(stage);
  const created = [];
  let indexPublished = false;
  try {
    for (const relative of newFiles) {
      if (relative === "index.html") continue;
      const source = path.join(stage, relative);
      const destination = path.join(dist, relative);
      try {
        const stat = await fsp.stat(destination);
        if (!stat.isFile() || !await sameBytes(source, destination)) {
          throw new Error(`dist_asset_name_collision:${relative}`);
        }
      } catch (error) {
        if (error?.code !== "ENOENT") throw error;
        await atomicCopy(source, destination);
        created.push(destination);
      }
    }
    if (failBeforeIndex) throw new Error("synthetic_failure_before_index_publish");
    await atomicCopy(path.join(stage, "index.html"), path.join(dist, "index.html"));
    indexPublished = true;
  } finally {
    if (!indexPublished) {
      for (const createdPath of created.reverse()) await fsp.rm(createdPath, { force: true }).catch(() => undefined);
    }
  }

  const keep = new Set(newFiles);
  for (const relative of oldFiles) {
    if (!keep.has(relative)) await fsp.rm(path.join(dist, relative), { force: true }).catch(() => undefined);
  }
}

async function main() {
  const dist = path.resolve(process.env.CHATGPT_ARCHIVE_DIST_DIR || path.join(webRoot, "dist"));
  const stage = await fsp.mkdtemp(path.join(path.dirname(dist), ".chatgpt-archive-dist-stage-"));
  try {
    const vite = path.join(webRoot, "node_modules", "vite", "bin", "vite.js");
    const result = spawnSync(process.execPath, [vite, "build", "--outDir", stage, "--emptyOutDir"], {
      cwd: webRoot,
      stdio: "inherit",
      shell: false,
    });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error(`vite_build_failed:${result.status}`);
    await publishStage(stage, dist);
  } finally {
    await fsp.rm(stage, { recursive: true, force: true }).catch(() => undefined);
  }
}

async function selfTest() {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), "chatgpt-archive-build-test-"));
  const stage = path.join(root, "stage");
  const dist = path.join(root, "dist");
  try {
    await fsp.mkdir(path.join(stage, "assets"), { recursive: true });
    await fsp.mkdir(path.join(dist, "assets"), { recursive: true });
    await fsp.writeFile(path.join(stage, "index.html"), '<script src="/assets/new.js"></script>');
    await fsp.writeFile(path.join(stage, "assets/new.js"), "new");
    await fsp.writeFile(path.join(dist, "index.html"), '<script src="/assets/old.js"></script>');
    await fsp.writeFile(path.join(dist, "assets/old.js"), "old");
    await assert.rejects(publishStage(stage, dist, { failBeforeIndex: true }), /synthetic_failure/);
    assert.match(await fsp.readFile(path.join(dist, "index.html"), "utf8"), /old\.js/);
    assert.equal(await fsp.readFile(path.join(dist, "assets/old.js"), "utf8"), "old");
    await assert.rejects(fsp.stat(path.join(dist, "assets/new.js")), { code: "ENOENT" });
    await publishStage(stage, dist);
    assert.match(await fsp.readFile(path.join(dist, "index.html"), "utf8"), /new\.js/);
    assert.equal(await fsp.readFile(path.join(dist, "assets/new.js"), "utf8"), "new");
    await assert.rejects(fsp.stat(path.join(dist, "assets/old.js")), { code: "ENOENT" });
  } finally {
    await fsp.rm(root, { recursive: true, force: true });
  }
}

if (path.resolve(process.argv[1] || "") === fileURLToPath(import.meta.url)) {
  if (process.argv.includes("--self-test")) await selfTest();
  else await main();
}
