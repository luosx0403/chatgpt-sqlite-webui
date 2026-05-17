import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(webRoot, "..");
const python = resolvePython();

function pythonCandidates(platform = process.platform, env = process.env) {
  if (env.PYTHON) return [{ command: env.PYTHON, args: [], kind: "env" }];
  if (platform === "win32") {
    return [
      { command: "python", args: [], kind: "python" },
      { command: "py", args: ["-3"], kind: "py-3" },
    ];
  }
  return [
    { command: "python3", args: [], kind: "python3" },
    { command: "python", args: [], kind: "python" },
  ];
}

function commandWorks(candidate) {
  const result = spawnSync(candidate.command, [...candidate.args, "--version"], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    shell: false,
  });
  return result.status === 0;
}

function resolvePython(platform = process.platform, env = process.env, works = commandWorks) {
  const candidates = pythonCandidates(platform, env);
  return candidates.find((candidate) => works(candidate)) || candidates[0];
}

function pythonCommand(candidate = python) {
  return [candidate.command, ...candidate.args];
}

function assertPythonResolution() {
  assert.deepEqual(pythonCandidates("win32", {}).map((candidate) => candidate.kind), ["python", "py-3"]);
  assert.deepEqual(pythonCandidates("linux", {}).map((candidate) => candidate.kind), ["python3", "python"]);
  assert.equal(resolvePython("win32", {}, (candidate) => candidate.kind === "py-3").kind, "py-3");
  assert.equal(resolvePython("linux", {}, (candidate) => candidate.kind === "python").kind, "python");
  assert.equal(resolvePython("win32", { PYTHON: "custom-python" }, () => true).kind, "env");
}

if (process.argv.includes("--self-test-python-resolution")) {
  assertPythonResolution();
  console.log("python_resolution ok");
  process.exit(0);
}

function browserExecutableCandidates() {
  return [
    process.env.CHROME_PATH,
    process.env.EDGE_PATH,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
  ].filter(Boolean);
}

function chromeExecutable() {
  return browserExecutableCandidates().find((candidate) => fs.existsSync(candidate));
}

async function launchBrowser() {
  assert.ok(
    browserExecutableCandidates().some((candidate) => String(candidate).includes("Microsoft\\Edge\\Application\\msedge.exe")),
    "browser candidates should include Windows Microsoft Edge paths",
  );
  const executablePath = chromeExecutable();
  try {
    const { chromium } = await import("playwright-core");
    return await chromium.launch({
      headless: true,
      executablePath,
      channel: executablePath ? undefined : "chrome",
    });
  } catch (error) {
    throw new Error(
      "Unable to launch Chrome, Chromium, or Microsoft Edge for DOM smoke. " +
      "Install one locally or set CHROME_PATH or EDGE_PATH to the browser executable. " +
      `Original error: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

function run(args, options = {}) {
  const result = spawnSync(args[0], args.slice(1), {
    cwd: repoRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    ...options,
  });
  if (result.status !== 0) {
    throw new Error(`${args.join(" ")} failed\n${result.stderr || result.stdout}`);
  }
  return result.stdout;
}

function node(nodeId, parent, role, text, ts, children = []) {
  return {
    id: nodeId,
    parent,
    children,
    message: {
      id: `msg-${nodeId}`,
      author: { role },
      create_time: ts,
      update_time: ts,
      content: { content_type: "text", parts: [text] },
    },
  };
}

function rawNode(nodeId, parent, role, content, ts, children = []) {
  return {
    id: nodeId,
    parent,
    children,
    message: {
      id: `msg-${nodeId}`,
      author: { role },
      create_time: ts,
      update_time: ts,
      content,
    },
  };
}

function root(children) {
  return { id: "root", parent: null, children, message: null };
}

function conversation(id, title, mapping, currentNode, ts) {
  return {
    id,
    conversation_id: `exported-${id}`,
    title,
    create_time: ts,
    update_time: ts + 100,
    current_node: currentNode,
    mapping,
  };
}

function sequenceNodeId(idx) {
  return `seq-${String(999 - idx).padStart(3, "0")}`;
}

function expectedSequenceHitIds(count = 180) {
  return Array.from({ length: count }, (_, idx) => sequenceNodeId(idx * 2));
}

function makeSyntheticConversations() {
  const conversations = [];
  const activeHitTerm = "needle-visible-target";
  const longHitFiller = Array.from({ length: 180 }, (_, idx) => `Synthetic filler line ${idx} keeps the active hit below the fold.`).join("\n");
  const longHitMapping = {
    root: root(["short-hit"]),
    "short-hit": node("short-hit", "root", "user", `Short synthetic hit for previous and next navigation: ${activeHitTerm}.`, 1_950_000_001, ["long-hit"]),
    "long-hit": node(
      "long-hit",
      "short-hit",
      "assistant",
      `${longHitFiller}\nThe visible active search target appears here: ${activeHitTerm}.\nMore deterministic trailing text after the target.`,
      1_950_000_010,
      [],
    ),
  };
  conversations.push(conversation("dom-active-hit", "DOM Active Hit Conversation", longHitMapping, "long-hit", 1_950_000_000));

  const titleOnlyMapping = {
    root: root(["u"]),
    u: node("u", "root", "user", "This synthetic body intentionally lacks the title-only query token.", 1_955_000_001),
  };
  conversations.push(conversation("dom-title-only", "title-only-target synthetic conversation", titleOnlyMapping, "u", 1_955_000_000));

  const roleClassMapping = {
    root: root(["tool"]),
    tool: node("tool", "root", "tool/system", "Synthetic tool-system internal text.", 1_956_000_001),
  };
  conversations.push(conversation("dom-role-class", "DOM Role Class Conversation", roleClassMapping, "tool", 1_956_000_000));

  const sequenceTerm = "sequence-target";
  const sequenceMapping = { root: root([sequenceNodeId(0)]) };
  for (let idx = 0; idx < 360; idx += 1) {
    const nodeId = sequenceNodeId(idx);
    const parent = idx === 0 ? "root" : sequenceNodeId(idx - 1);
    const child = idx < 359 ? sequenceNodeId(idx + 1) : null;
    const hasSequenceHit = idx % 2 === 0;
    const variableHeightText = hasSequenceHit && idx < 120
      ? `${Array.from({ length: 90 }, (_, line) => `Variable height synthetic line ${idx}.${line}`).join("\n")}\n${sequenceTerm} visual hit ${idx}`
      : hasSequenceHit
        ? `Short synthetic hit ${idx}: ${sequenceTerm}`
        : `Short synthetic filler ${idx}`;
    sequenceMapping[nodeId] = node(
      nodeId,
      parent,
      idx % 2 === 0 ? "user" : "assistant",
      variableHeightText,
      1_960_000_000 + (95 - idx),
      child ? [child] : [],
    );
  }
  conversations.push(conversation("dom-hit-sequence", "DOM Hit Sequence Conversation", sequenceMapping, sequenceNodeId(359), 1_960_000_000));

  const longMapping = { root: root(["sys"]) };
  longMapping.sys = rawNode("sys", "root", "system", { content_type: "text", text: "Synthetic system context for DOM test" }, 1_900_000_001, ["ctx"]);
  longMapping.ctx = rawNode(
    "ctx",
    "sys",
    "user",
    { content_type: "user_editable_context", user_profile: "Synthetic profile text", user_instructions: { text: "Synthetic raw preview instructions" } },
    1_900_000_002,
    ["n0"],
  );
  let previous = "ctx";
  for (let idx = 0; idx < 380; idx += 1) {
    const nodeId = `n${idx}`;
    const child = idx < 379 ? `n${idx + 1}` : null;
    const text = [
      `Synthetic message ${idx} with sqlite3 and Python 3.13 tokens.`,
      idx === 120 ? "This row contains 中文关键词 and 繁體關鍵詞 for highlight checks." : "",
      idx === 240 ? "Command sample: python -m unittest discover --no-input-sha256." : "",
    ].filter(Boolean).join("\n");
    longMapping[nodeId] = node(nodeId, previous, idx % 2 === 0 ? "user" : "assistant", text, 1_900_000_010 + idx, child ? [child] : []);
    previous = nodeId;
  }
  conversations.push(conversation("dom-long", "DOM Long Conversation", longMapping, "n379", 1_900_000_000));

  for (let idx = 0; idx < 150; idx += 1) {
    const id = `dom-${String(idx).padStart(3, "0")}`;
    const mapping = {
      root: root(["u"]),
      u: node("u", "root", "user", `Synthetic searchable title ${idx} C++ C# gpt-5.5`, 1_800_000_000 + idx, ["a"]),
      a: node("a", "u", "assistant", `Synthetic response ${idx} with sqlite3 token and 中文关键词.`, 1_800_000_100 + idx),
    };
    conversations.push(conversation(id, `Synthetic Conversation ${idx}`, mapping, "a", 1_800_000_000 + idx));
  }
  return conversations;
}

function writeZipFile(zipPath, conversations) {
  const jsonPath = `${zipPath}.json`;
  fs.writeFileSync(jsonPath, JSON.stringify(conversations), "utf8");
  run([
    ...pythonCommand(),
    "-c",
    "import pathlib, sys, zipfile; z=pathlib.Path(sys.argv[1]); j=pathlib.Path(sys.argv[2]); z.parent.mkdir(parents=True, exist_ok=True); zipfile.ZipFile(z,'w').writestr('conversations.json', j.read_text(encoding='utf-8'))",
    zipPath,
    jsonPath,
  ]);
  fs.unlinkSync(jsonPath);
}

async function waitForHealth(baseUrl) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(new URL("/api/health", baseUrl));
      if (response.ok && (await response.json()).ok) return;
    } catch {
      // Server is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error("web server did not become healthy");
}

async function waitForCount(page, selector, min) {
  await page.waitForFunction(
    ({ selector: css, min: expected }) => document.querySelectorAll(css).length >= expected,
    { selector, min },
    { timeout: 20_000 },
  );
}

function parseRgb(value) {
  const match = value.match(/rgba?\(([^)]+)\)/);
  assert.ok(match, `expected rgb color, got ${value}`);
  return match[1].split(",").slice(0, 3).map((part) => Number.parseFloat(part.trim()) / 255);
}

function relativeLuminance([r, g, b]) {
  const linear = [r, g, b].map((channel) => (
    channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  ));
  return (0.2126 * linear[0]) + (0.7152 * linear[1]) + (0.0722 * linear[2]);
}

function contrastRatio(foreground, background) {
  const l1 = relativeLuminance(parseRgb(foreground));
  const l2 = relativeLuminance(parseRgb(background));
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
}

async function waitForActiveHighlightVisible(page) {
  await page.waitForFunction(() => {
    const scroller = document.querySelector(".message-scroll");
    const mark = document.querySelector(".message-active .search-highlight-active, .message-active .search-highlight");
    if (!scroller || !mark) return false;
    const scrollRect = scroller.getBoundingClientRect();
    const markRect = mark.getBoundingClientRect();
    return markRect.top >= scrollRect.top && markRect.bottom <= scrollRect.bottom && markRect.height > 0;
  }, undefined, { timeout: 20_000 });
}

async function activeHighlightContrast(page) {
  const styles = await page.locator(".message-active .search-highlight-active, .message-active .search-highlight").first().evaluate((node) => {
    const computed = window.getComputedStyle(node);
    return { color: computed.color, backgroundColor: computed.backgroundColor };
  });
  return contrastRatio(styles.color, styles.backgroundColor);
}

async function activeNodeId(page) {
  return page.locator(".message-active").first().evaluate((node) => node.getAttribute("data-node-id")).catch(() => null);
}

async function waitForActiveNodeWithVisibleHighlight(page, nodeId) {
  await page.waitForFunction(
    (expected) => document.querySelector(".message-active")?.getAttribute("data-node-id") === expected,
    nodeId,
    { timeout: 20_000 },
  );
  await waitForActiveHighlightVisible(page);
  assert.equal(await activeNodeId(page), nodeId);
}

async function activateHitNode(page, nodeId) {
  for (let attempt = 0; attempt < 4; attempt += 1) {
    await waitForActiveHighlightVisible(page);
    if (await activeNodeId(page) === nodeId) return;
    await page.getByRole("button", { name: "Next hit" }).click();
  }
  assert.equal(await activeNodeId(page), nodeId, `expected active hit node ${nodeId}`);
}

async function main() {
  assertPythonResolution();
  const distIndex = path.join(webRoot, "dist", "index.html");
  assert.ok(fs.existsSync(distIndex), "webui/dist/index.html is missing; run npm run build before npm run test:dom");

  const tmp = await fsp.mkdtemp(path.join(os.tmpdir(), "chatgpt-export-archiver-dom-"));
  let server;
  let noDbServer;
  let browser;
  try {
    browser = await launchBrowser();
    const uploadZip = path.join(tmp, "upload.zip");
    const uploadMapping = {
      root: root(["u"]),
      u: node("u", "root", "user", "Synthetic upload import text.", 1_970_000_001),
    };
    writeZipFile(uploadZip, [conversation("dom-upload", "DOM Upload Conversation", uploadMapping, "u", 1_970_000_000)]);
    const noDb = path.join(tmp, "new-archive.db");
    const noDbPort = 17_000 + Math.floor(Math.random() * 1000);
    const noDbUrl = `http://127.0.0.1:${noDbPort}/`;
    noDbServer = spawn(python.command, [...python.args, "chatgpt_archive.py", "web", "--db", noDb, "--host", "127.0.0.1", "--port", String(noDbPort)], {
      cwd: repoRoot,
      stdio: ["ignore", "pipe", "pipe"],
    });
    noDbServer.stdout.on("data", () => undefined);
    noDbServer.stderr.on("data", () => undefined);
    await waitForHealth(noDbUrl);
    const noDbContext = await browser.newContext({ viewport: { width: 1100, height: 760 }, locale: "en-US" });
    const noDbPage = await noDbContext.newPage();
    await noDbPage.goto(noDbUrl, { waitUntil: "networkidle" });
    assert.equal(await noDbPage.locator("text=Fallback UI").count(), 0, "no-db web should serve React UI, not fallback");
    await noDbPage.getByTestId("import-panel").waitFor({ state: "visible", timeout: 20_000 });
    await noDbPage.getByTestId("import-zip-input").setInputFiles(uploadZip);
    await noDbPage.getByTestId("import-start-button").click();
    await noDbPage.waitForFunction(() => document.querySelector('[data-testid="import-status"]')?.textContent?.includes("succeeded"), undefined, { timeout: 60_000 });
    await noDbPage.waitForFunction(() => document.querySelectorAll(".conversation-item").length >= 1, undefined, { timeout: 20_000 });
    await noDbContext.close();
    if (noDbServer.exitCode === null && noDbServer.signalCode === null) {
      noDbServer.kill("SIGTERM");
      await new Promise((resolve) => noDbServer.once("exit", resolve));
      noDbServer = undefined;
    }

    const inputDir = path.join(tmp, "input");
    await fsp.mkdir(inputDir);
    await fsp.writeFile(path.join(inputDir, "conversations.json"), JSON.stringify(makeSyntheticConversations()), "utf8");
    const db = path.join(tmp, "archive.db");
    run([...pythonCommand(), "chatgpt_archive.py", "import", "--db", db, "--input", inputDir, "--no-input-sha256"]);
    run([...pythonCommand(), "chatgpt_archive.py", "web-index", "--db", db]);

    const port = 19_000 + Math.floor(Math.random() * 2000);
    const baseUrl = `http://127.0.0.1:${port}/`;
    server = spawn(python.command, [...python.args, "chatgpt_archive.py", "web", "--db", db, "--host", "127.0.0.1", "--port", String(port)], {
      cwd: repoRoot,
      stdio: ["ignore", "pipe", "pipe"],
    });
    server.stdout.on("data", () => undefined);
    server.stderr.on("data", () => undefined);
    await waitForHealth(baseUrl);

    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "en-US" });
    const page = await context.newPage();
    await page.addInitScript(() => {
      window.__copiedText = "";
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: async (text) => {
            window.__copiedText = text;
          },
        },
      });
    });
    const browserDiagnostics = [];
    page.on("console", (message) => browserDiagnostics.push(`${message.type()}: ${message.text()}`));
    page.on("pageerror", (error) => browserDiagnostics.push(`pageerror: ${error.message}`));
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    try {
      await waitForCount(page, ".conversation-item", 20);
    } catch (error) {
      const health = await (await fetch(new URL("/api/health", baseUrl))).json();
      const apiPage = await (await fetch(new URL("/api/conversations?limit=5&sort=newest", baseUrl))).json();
      throw new Error(`initial conversation items did not render; health=${JSON.stringify(health)} api_count=${apiPage.items?.length ?? 0} diagnostics=${browserDiagnostics.join(" | ")}`);
    }

    let delayedProgressRequest = false;
    await page.route("**/api/conversations**", async (route) => {
      const url = new URL(route.request().url());
      if (!delayedProgressRequest && url.pathname === "/api/conversations" && url.searchParams.get("q") === "progress-target") {
        delayedProgressRequest = true;
        await new Promise((resolve) => setTimeout(resolve, 700));
      }
      await route.continue();
    });
    await page.locator("#global-search").fill("progress-target");
    const progress = page.getByTestId("search-loading-progress");
    await progress.waitFor({ state: "visible", timeout: 20_000 });
    const firstProgressText = await progress.textContent();
    assert.ok(firstProgressText?.includes("[") && firstProgressText.includes("]"), "loading progress should look like a text bar");
    assert.ok(firstProgressText?.includes("█"), "loading progress should use visible block characters");
    await page.waitForFunction(
      (previous) => {
        const node = document.querySelector('[data-testid="search-loading-progress"]');
        return Boolean(node?.textContent && node.textContent !== previous);
      },
      firstProgressText,
      { timeout: 2_000 },
    );
    await progress.waitFor({ state: "hidden", timeout: 20_000 });
    await page.waitForFunction(() => document.querySelector(".results-meta")?.textContent?.includes("0 of 0 conversations"), undefined, { timeout: 10_000 });
    await page.unroute("**/api/conversations**");
    await page.locator("#global-search").fill("");
    await waitForCount(page, ".conversation-item", 20);

    const listMetrics = await page.locator(".conversation-list").evaluate((node) => ({
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      before: node.scrollTop,
    }));
    assert.ok(listMetrics.scrollHeight > listMetrics.clientHeight, "conversation list must scroll internally");
    await page.locator(".conversation-list").evaluate((node) => { node.scrollTop = 300; });
    await page.waitForFunction(() => document.querySelector(".conversation-list")?.scrollTop > 0);

    const beforeItems = await page.locator(".conversation-item").count();
    await page.locator(".conversation-list").evaluate((node) => { node.scrollTop = node.scrollHeight; });
    try {
      await waitForCount(page, ".conversation-item", beforeItems + 1);
    } catch {
      await page.getByRole("button", { name: "Load more" }).click();
      await waitForCount(page, ".conversation-item", beforeItems + 1);
    }
    const afterItems = await page.locator(".conversation-item").count();
    assert.ok(afterItems > beforeItems, "Load more should append conversations");

    await page.goto(`${baseUrl}?conversation=dom-long`, { waitUntil: "networkidle" });
    try {
      await waitForCount(page, ".message", 1);
      await page.waitForFunction(() => document.querySelector(".message-page-meta")?.textContent?.includes("300 of 383 messages"), undefined, { timeout: 20_000 });
    } catch (error) {
      const apiMessages = await (await fetch(new URL("/api/conversations/dom-long/messages?limit=5", baseUrl))).json();
      const readerText = await page.locator(".reader").textContent({ timeout: 1000 }).catch(() => "");
      throw new Error(`long conversation messages did not render; api_count=${apiMessages.items?.length ?? 0} total=${apiMessages.total ?? "unknown"} reader=${JSON.stringify((readerText || "").slice(0, 160))} diagnostics=${browserDiagnostics.join(" | ")}`);
    }
    const messageMetrics = await page.locator(".message-scroll").evaluate((node) => ({
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      before: node.scrollTop,
    }));
    assert.ok(messageMetrics.scrollHeight > messageMetrics.clientHeight, "message list must scroll internally");
    await page.locator(".message-scroll").evaluate((node) => { node.scrollTop = 500; });
    await page.waitForFunction(() => document.querySelector(".message-scroll")?.scrollTop > 0);

    await page.getByRole("button", { name: "Copy conversation" }).click();
    await page.waitForFunction(() => window.__copiedText?.includes("Synthetic message 379"), undefined, { timeout: 20_000 });

    await page.getByRole("button", { name: "Load more messages" }).click();
    await page.waitForFunction(() => document.querySelector(".message-page-meta")?.textContent?.includes("383 of 383 messages"), undefined, { timeout: 20_000 });

    const showRawCount = await page.getByRole("button", { name: "Show raw preview" }).count();
    assert.ok(showRawCount > 0, "raw preview toggle should be available");
    await page.getByRole("button", { name: "Show raw preview" }).first().click();
    await page.locator(".raw-message").first().waitFor({ state: "visible", timeout: 10_000 });

    await page.locator(".message-scroll").evaluate((node) => { node.scrollTop = 0; });
    await page.getByLabel("Show internal messages").check();
    await page.waitForFunction(
      () => Array.from(document.querySelectorAll(".message-text")).some((node) => node.textContent?.includes("Synthetic system context")),
      undefined,
      { timeout: 20_000 },
    );

    await page.locator("#global-search").fill("sqlite3");
    await page.waitForFunction(() => document.querySelectorAll(".search-highlight").length > 0, undefined, { timeout: 20_000 });

    await page.locator("#global-search").fill("a/b");
    await page.waitForFunction(() => document.querySelector("#global-search")?.value === "a/b", undefined, { timeout: 10_000 });

    await page.goto(`${baseUrl}?conversation=dom-role-class`, { waitUntil: "networkidle" });
    await page.getByLabel("Show internal messages").check();
    await page.waitForFunction(() => document.querySelector(".message-role-tool-system"), undefined, { timeout: 20_000 });
    const toolClassName = await page.locator(".message-role-tool-system").first().evaluate((node) => node.className);
    assert.ok(!toolClassName.includes("/"), "message role classes must be CSS-safe");

    await page.goto(`${baseUrl}?conversation=dom-title-only`, { waitUntil: "networkidle" });
    await page.locator("details.advanced-panel").evaluate((node) => { node.open = true; });
    await page.getByLabel("Scope").selectOption("title");
    await page.locator("#global-search").fill("title-only-target");
    await page.waitForFunction(() => document.querySelector(".reader-header h1")?.textContent?.includes("title-only-target"), undefined, { timeout: 20_000 });
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("No hits"), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".search-highlight").count(), 0);

    await page.goto(`${baseUrl}?conversation=dom-active-hit`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    await page.locator("#global-search").fill("needle-visible-target");
    await page.waitForFunction(() => document.querySelectorAll(".search-highlight").length > 0, undefined, { timeout: 20_000 });
    await activateHitNode(page, "long-hit");
    await waitForActiveHighlightVisible(page);
    await page.getByRole("button", { name: "Next hit" }).click();
    await waitForActiveHighlightVisible(page);
    await page.getByRole("button", { name: "Prev hit" }).click();
    await activateHitNode(page, "long-hit");
    await waitForActiveHighlightVisible(page);

    await page.goto(`${baseUrl}?conversation=dom-hit-sequence`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    await page.locator("#global-search").fill("sequence-target");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("1 / 180"), undefined, { timeout: 20_000 });
    const expectedSequence = expectedSequenceHitIds();
    for (let idx = 0; idx <= 155; idx += 1) {
      await waitForActiveNodeWithVisibleHighlight(page, expectedSequence[idx]);
      if (idx < 155) await page.getByRole("button", { name: "Next hit" }).click();
    }

    await page.getByRole("button", { name: "Settings" }).click();
    await page.locator(".settings-modal select").nth(1).selectOption("dark");
    await page.getByRole("button", { name: "Close" }).click();
    await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
    assert.ok(await activeHighlightContrast(page) >= 4.5, "dark search highlight contrast should be readable");
    await page.getByRole("button", { name: "Settings" }).click();
    await page.locator(".settings-modal select").nth(1).selectOption("light");
    await page.getByRole("button", { name: "Close" }).click();
    await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
    assert.ok(await activeHighlightContrast(page) >= 4.5, "light search highlight contrast should be readable");

    const selectedBeforeEmpty = await page.locator(".reader-header h1").textContent();
    assert.ok((selectedBeforeEmpty || "").trim().length > 0, "reader should have a selected conversation before empty search");
    await page.locator("#global-search").fill("zzzzzzzzzzqqqqqqqq");
    await page.locator(".empty-state").waitFor({ state: "visible", timeout: 20_000 });

    await page.getByRole("button", { name: "Settings" }).click();
    await page.locator(".settings-modal select").first().selectOption("zh-Hans");
    await page.getByRole("button", { name: "关闭" }).click();
    await page.reload({ waitUntil: "networkidle" });
    await page.getByRole("button", { name: "设置" }).waitFor({ state: "visible", timeout: 20_000 });

    await page.setViewportSize({ width: 390, height: 800 });
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    await waitForCount(page, ".conversation-item", 5);
    const narrow = await page.locator(".message-scroll, .empty-state").first().boundingBox();
    assert.ok(narrow && narrow.height > 100, "narrow layout should keep reader usable");

    console.log("dom_smoke ok");
  } finally {
    if (browser) await browser.close();
    if (server) {
      if (server.exitCode === null && server.signalCode === null) {
        server.kill("SIGTERM");
        await new Promise((resolve) => server.once("exit", resolve));
      }
    }
    if (noDbServer) {
      if (noDbServer.exitCode === null && noDbServer.signalCode === null) {
        noDbServer.kill("SIGTERM");
        await new Promise((resolve) => noDbServer.once("exit", resolve));
      }
    }
    await fsp.rm(tmp, { recursive: true, force: true });
  }
}

await main();
