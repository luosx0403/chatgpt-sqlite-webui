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
  if (result.error) return false;
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
  assert.equal(commandWorks({ command: path.join(os.tmpdir(), "missing-python-for-dom-smoke"), args: [], kind: "env" }), false);
}

function assertStaticFrontendContracts() {
  const appSource = fs.readFileSync(path.join(webRoot, "src/App.tsx"), "utf8");
  const clientSource = fs.readFileSync(path.join(webRoot, "src/api/client.ts"), "utf8");
  const paneSource = fs.readFileSync(path.join(webRoot, "src/components/ConversationPane.tsx"), "utf8");
  const querySyntaxSource = fs.readFileSync(path.join(webRoot, "src/utils/querySyntax.ts"), "utf8");
  const i18nSource = fs.readFileSync(path.join(webRoot, "src/i18n.ts"), "utf8");
  assert.ok(appSource.includes('web_index_recovery: t("stageWebIndexRecovery")'), "web-index-recovery import stage should use a localized label");
  assert.ok(appSource.includes("has_internal_hits: meta.has_internal_hits"), "selected conversation merge must preserve hidden/internal search metadata after detail load");
  assert.ok(appSource.includes("void has_internal_hits"), "selected conversation metadata clear must remove stale internal search metadata");
  assert.ok(clientSource.includes("count_total"), "message hit client should expose count_total for fast navigation requests");
  assert.ok(paneSource.includes("countTotal: false"), "reader hit navigation should request fast message-hit pages without exact total counts");
  assert.ok(paneSource.includes("visible_total"), "reader should consume visible message totals from the API");
  assert.ok(paneSource.includes("effectivePath"), "reader download/copy/navigation should use effective query path");
  assert.ok(clientSource.includes("include_internal"), "reader download links should pass current internal visibility");
  assert.ok(paneSource.includes('disabled={Boolean(querySyntax.pathOverride)}'), "overridden path select should not look interactive");
  assert.ok(querySyntaxSource.includes("toLocaleLowerCase"), "frontend query syntax should case-fold modifier values like the backend");
  assert.ok(querySyntaxSource.includes("readQuoted"), "frontend query syntax should parse quoted modifier values");
  assert.ok(i18nSource.includes("stageWebIndexRecovery"), "web-index-recovery stage label should be translated");
  assert.ok(i18nSource.includes("UTC calendar days"), "date filter UTC wording should be visible in search help");
  assert.ok(i18nSource.includes("preparingCopy"), "copy loading state should be localized");
}

if (process.argv.includes("--self-test-python-resolution")) {
  assertPythonResolution();
  assertStaticFrontendContracts();
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
  if (result.error) {
    throw new Error(`${args.join(" ")} failed to start: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`${args.join(" ")} failed\n${result.stderr || result.stdout || "no process output"}`);
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

  const branchOverrideMapping = {
    root: root(["u"]),
    u: node("u", "root", "user", "Current path body without the branch override token.", 1_955_200_001, ["a", "branch"]),
    a: node("a", "u", "assistant", "Current answer body.", 1_955_200_002),
    branch: node("branch", "u", "assistant", "branchoverride-token appears only on a branch.", 1_955_200_003),
  };
  conversations.push(conversation("dom-branch-override", "DOM Branch Override Conversation", branchOverrideMapping, "a", 1_955_200_000));

  const readerFilterMapping = {
    root: root(["u"]),
    u: node("u", "root", "user", "filtertarget user body", 1_955_500_001, ["a"]),
    a: node("a", "u", "assistant", "filtertarget assistant body", 1_955_500_002, ["b"]),
    b: node("b", "a", "assistant", "filtertarget excluded body", 1_955_500_003, ["c"]),
    c: node("c", "b", "assistant", 'filtertarget exact phrase foo "bar" and backslash \\ marker', 1_955_500_004),
  };
  conversations.push(conversation("dom-reader-filter", "DOM Reader Filter Conversation titleblock", readerFilterMapping, "c", 1_955_500_000));

  const roleClassMapping = {
    root: root(["tool"]),
    tool: node("tool", "root", "tool/system", "Synthetic tool-system internal text.", 1_956_000_001),
  };
  conversations.push(conversation("dom-role-class", "DOM Role Class Conversation", roleClassMapping, "tool", 1_956_000_000));

  const techJsonMapping = {
    root: root(["u"]),
    u: node("u", "root", "user", '{"ordinary":"user json should stay as a normal chat message"}', 1_957_000_001, ["a"]),
    a: node("a", "u", "assistant", "```json\n{\"ordinary\":\"assistant code block should stay readable\"}\n```", 1_957_000_002, ["q"]),
    q: node("q", "a", "assistant", '{"query":"synthetic archive question","answer":{"data":[1,2,3],"title":"ordinary JSON result"}}', 1_957_000_003, ["b"]),
    b: node("b", "q", "assistant", '{"search_query":[{"q":"synthetic docs"}],"response_length":"short"}', 1_957_000_004, ["c"]),
    c: rawNode("c", "b", "assistant", { content_type: "thoughts", text: "source analysis msg id: synthetic-source-analysis-id", extra: "x".repeat(1200) }, 1_957_000_004),
  };
  conversations.push(conversation("dom-tech-json", "DOM Technical JSON Conversation", techJsonMapping, "c", 1_957_000_000));

  const intelMapping = {
    root: root(["u"]),
    u: node("u", "root", "user", "Intel token appears as a complete word. Intel(R) and Intel. should both count.", 1_958_000_001, ["a"]),
    a: node("a", "u", "assistant", "Synthetic response after the whole-word hit.", 1_958_000_002),
  };
  conversations.push(conversation("dom-word-intel", "DOM Intel Word Conversation", intelMapping, "a", 1_958_000_000));

  const intelligenceMapping = {
    root: root(["u"]),
    u: node("u", "root", "assistant", "Intelligence and IntelliSense contain the same letters but are longer words.", 1_958_100_001),
  };
  conversations.push(conversation("dom-word-intelligence", "DOM Intelligence Longer Conversation", intelligenceMapping, "u", 1_958_100_000));

  const zhOverlapTerm = "英特尔";
  const zhLongLines = Array.from({ length: 32 }, (_, idx) => `Synthetic ${zhOverlapTerm} search layout line ${idx} keeps this assistant bubble tall for geometry checks.`).join("\n");
  const zhOverlapMapping = {
    root: root(["u0"]),
    u0: node("u0", "root", "user", `Synthetic user question with ${zhOverlapTerm} near the start.`, 1_958_200_001, ["a0"]),
    a0: node("a0", "u0", "assistant", `${zhLongLines}\nFinal ${zhOverlapTerm} hit in the first long assistant message.`, 1_958_200_002, ["a1"]),
    a1: node("a1", "a0", "assistant", "Synthetic short assistant status message between long search hits.", 1_958_200_003, ["u1"]),
    u1: node("u1", "a1", "user", `A compact user bubble also mentions ${zhOverlapTerm}.`, 1_958_200_004, ["a2"]),
    a2: node("a2", "u1", "assistant", `${zhLongLines}\nAnother ${zhOverlapTerm} hit in a later long assistant message.`, 1_958_200_005),
  };
  conversations.push(conversation("dom-zh-overlap", "DOM Chinese Search Geometry Conversation", zhOverlapMapping, "a2", 1_958_200_000));
  const zhInternalMapping = {
    root: root(["sys"]),
    sys: node(
      "sys",
      "root",
      "system",
      [
        "[2026/03/13, 21:00:59] - youtoob🔥: synthetic warmup",
        "🔥🔥 youtoob🔥: 英特尔有资源",
      ].join("\n"),
      1_958_205_001,
      ["a"],
    ),
    a: node("a", "sys", "assistant", "Visible response without the hidden search token.", 1_958_205_002),
  };
  conversations.push(conversation("dom-zh-internal-only", "DOM Chinese Internal Hidden Result", zhInternalMapping, "a", 1_958_205_000));
  const multiscriptMapping = {
    root: root(["u"]),
    u: node(
      "u",
      "root",
      "user",
      [
        "Multiscript word-mode fixture: python Intel Intel(R) gpt-5.5.",
        "Japanese かなテスト and Korean 한글테스트 should stay searchable.",
        "Fullwidth Ｉｎｔｅｌ and ｐｙｔｈｏｎ normalize for reader highlights.",
        "Mixed phrase 英特尔 Intel plus emoji prefix 🔥🔥 marker before text.",
        "Whitespace phrase fixture keeps foo\nbar searchable as an exact phrase.",
      ].join("\n"),
      1_958_206_001,
      ["a"],
    ),
    a: node("a", "u", "assistant", "Intelligence and Intellicode are longer words for whole-word negative checks.", 1_958_206_002),
  };
  conversations.push(conversation("dom-multiscript", "DOM Multiscript Search Conversation", multiscriptMapping, "a", 1_958_206_000));
  for (let idx = 0; idx < 3; idx += 1) {
    const mapping = {
      root: root(["u"]),
      u: node("u", "root", "user", `Synthetic ${zhOverlapTerm} result ${idx} for switching search results.`, 1_958_210_001 + idx, ["a"]),
      a: node("a", "u", "assistant", `Short response with ${zhOverlapTerm} result ${idx}.`, 1_958_210_101 + idx),
    };
    conversations.push(conversation(`dom-zh-overlap-${idx}`, `DOM Chinese Search Geometry Result ${idx}`, mapping, "a", 1_958_210_000 + idx));
  }

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

async function assertHighlightsEqual(page, expected, label) {
  const texts = await page.locator(".search-highlight").evaluateAll((nodes) => nodes.map((node) => node.textContent || ""));
  assert.ok(texts.length > 0, `${label} should render search highlights`);
  assert.ok(texts.every((text) => text === expected), `${label} highlight range must align with JS UTF-16 slicing after emoji; got ${JSON.stringify(texts)}`);
}

async function assertHighlightsAllowed(page, allowed, label) {
  const texts = await page.locator(".search-highlight").evaluateAll((nodes) => nodes.map((node) => node.textContent || ""));
  assert.ok(texts.length > 0, `${label} should render search highlights`);
  assert.ok(texts.every((text) => allowed.includes(text)), `${label} highlight range must align with allowed original text; got ${JSON.stringify(texts)}`);
}

async function waitForHighlightsAllowed(page, allowed) {
  await page.waitForFunction((values) => {
    const allowedTexts = new Set(values);
    const texts = Array.from(document.querySelectorAll(".search-highlight")).map((node) => node.textContent || "");
    return texts.length > 0 && texts.every((text) => allowedTexts.has(text));
  }, allowed, { timeout: 20_000 });
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

async function assertNoLargeMessageGap(page, label) {
  const result = await messageViewportMetrics(page);
  assert.ok(result.gapOk, `${label} should not have a large virtualized blank gap; maxGap=${result.maxGap} visibleRows=${result.visibleRows}`);
}

async function messageViewportMetrics(page) {
  return page.locator(".message-scroll").evaluate((node) => {
    const scroller = node.getBoundingClientRect();
    const rows = Array.from(node.querySelectorAll(".virtual-row"))
      .map((row) => row.getBoundingClientRect())
      .filter((row) => row.height > 0 && row.bottom > scroller.top && row.top < scroller.bottom)
      .sort((a, b) => a.top - b.top);
    const bubbles = Array.from(node.querySelectorAll(".message"))
      .map((bubble) => bubble.getBoundingClientRect())
      .filter((bubble) => bubble.height > 0 && bubble.width > 0 && bubble.bottom > scroller.top && bubble.top < scroller.bottom)
      .sort((a, b) => a.top - b.top);
    const hasOuterShadow = (selector) => Array.from(node.querySelectorAll(selector)).some((element) => {
      const shadow = window.getComputedStyle(element).boxShadow;
      return shadow !== "none" && /\)\s+0px\s+0px\s+0px\s+\d+px(?!\s+inset)/.test(shadow);
    });
    if (!rows.length) {
      return {
        gapOk: false,
        overlapOk: false,
        bubbleOverlapOk: false,
        activeVisualOk: false,
        maxGap: scroller.height,
        maxOverlap: 0,
        maxBubbleOverlap: 0,
        visibleRows: 0,
        visibleBubbles: bubbles.length,
        horizontalOverflow: node.scrollWidth > node.clientWidth,
      };
    }
    let maxGap = Math.max(0, rows[0].top - scroller.top);
    let maxOverlap = 0;
    for (let idx = 1; idx < rows.length; idx += 1) {
      maxGap = Math.max(maxGap, rows[idx].top - rows[idx - 1].bottom);
      maxOverlap = Math.max(maxOverlap, rows[idx - 1].bottom - rows[idx].top);
    }
    let maxBubbleOverlap = 0;
    for (let idx = 1; idx < bubbles.length; idx += 1) {
      maxBubbleOverlap = Math.max(maxBubbleOverlap, bubbles[idx - 1].bottom - bubbles[idx].top);
    }
    maxGap = Math.max(maxGap, scroller.bottom - rows[rows.length - 1].bottom);
    const scrollable = node.scrollHeight > node.clientHeight + 4;
    return {
      gapOk: !scrollable || maxGap < Math.min(260, scroller.height * 0.35),
      overlapOk: maxOverlap <= 1,
      bubbleOverlapOk: maxBubbleOverlap <= 2,
      activeVisualOk: !hasOuterShadow(".message-active, .search-highlight-active"),
      maxGap,
      maxOverlap,
      maxBubbleOverlap,
      visibleRows: rows.length,
      visibleBubbles: bubbles.length,
      rowsSummary: rows.map((row) => ({ top: Math.round(row.top), bottom: Math.round(row.bottom), height: Math.round(row.height) })).slice(0, 8),
      horizontalOverflow: node.scrollWidth > node.clientWidth,
    };
  });
}

async function assertNoMessageOverlap(page, label) {
  const result = await messageViewportMetrics(page);
  assert.ok(result.overlapOk, `${label} should not overlap virtual rows; maxOverlap=${result.maxOverlap} visibleRows=${result.visibleRows} rows=${JSON.stringify(result.rowsSummary || [])}`);
}

async function assertStableMessageViewport(page, label) {
  let result = await messageViewportMetrics(page);
  for (let attempt = 0; attempt < 12 && (!result.gapOk || !result.overlapOk || !result.bubbleOverlapOk || !result.activeVisualOk || result.horizontalOverflow); attempt += 1) {
    await page.waitForTimeout(80);
    result = await messageViewportMetrics(page);
  }
  assert.ok(result.gapOk, `${label} should not have a large virtualized blank gap; maxGap=${result.maxGap} visibleRows=${result.visibleRows}`);
  assert.ok(result.overlapOk, `${label} should not overlap virtual rows; maxOverlap=${result.maxOverlap} visibleRows=${result.visibleRows} rows=${JSON.stringify(result.rowsSummary || [])}`);
  assert.ok(result.bubbleOverlapOk, `${label} should not overlap visible message bubbles; maxBubbleOverlap=${result.maxBubbleOverlap} visibleBubbles=${result.visibleBubbles}`);
  assert.ok(result.activeVisualOk, `${label} active hit styles should not use outer visual shadows`);
  assert.equal(result.horizontalOverflow, false, `${label} should not create page-level horizontal message scrolling`);
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
  assertStaticFrontendContracts();
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
    await page.getByRole("button", { name: "Search help" }).click();
    await page.getByRole("dialog", { name: "Search help" }).waitFor({ state: "visible", timeout: 20_000 });
    assert.ok((await page.getByRole("dialog", { name: "Search help" }).textContent())?.includes("UTC calendar days"), "search help should state date filters use UTC calendar days");
    await page.getByRole("button", { name: "Close" }).click();

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

    const matchModeRequests = [];
    await page.route("**/api/conversations**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/conversations" && url.searchParams.get("q") === "Intel") {
        matchModeRequests.push(url.searchParams.get("match_mode") || "contains");
      }
      await route.continue();
    });
    await page.locator("#global-search").fill("Intel");
    await page.waitForFunction(() => document.querySelector(".results-meta")?.textContent?.includes("3 of 3 conversations"), undefined, { timeout: 20_000 });
    assert.ok(await page.getByRole("button", { name: /DOM Intel Word Conversation/ }).count() === 1, "contains mode should include the complete Intel conversation");
    assert.ok(await page.getByRole("button", { name: /DOM Intelligence Longer Conversation/ }).count() === 1, "contains mode should include the longer word conversation");
    await page.getByLabel("Whole word").check();
    await page.waitForFunction(() => document.querySelector(".results-meta")?.textContent?.includes("2 of 2 conversations"), undefined, { timeout: 20_000 });
    assert.ok(matchModeRequests.includes("word"), "whole-word UI toggle should send match_mode=word");
    assert.ok(await page.getByRole("button", { name: /DOM Intel Word Conversation/ }).count() === 1, "whole-word mode should keep the complete Intel conversation");
    assert.ok(await page.getByRole("button", { name: /DOM Multiscript Search Conversation/ }).count() === 1, "whole-word mode should keep the multiscript complete Intel conversation");
    assert.equal(await page.getByRole("button", { name: /DOM Intelligence Longer Conversation/ }).count(), 0, "whole-word mode should drop longer-word matches");
    await waitForCount(page, ".search-highlight", 1);
    const highlightedText = await page.locator(".search-highlight").first().textContent();
    assert.equal((highlightedText || "").toLowerCase(), "intel", "whole-word highlighting should mark the complete token only");
    await assertStableMessageViewport(page, "whole-word search result selection");
    for (const sample of [
      { query: "かなテスト", expected: ["かなテスト"], label: "Japanese whole-word search", resultText: "1 of 1 conversations" },
      { query: "한글테스트", expected: ["한글테스트"], label: "Korean whole-word search", resultText: "1 of 1 conversations" },
      { query: "Ｉｎｔｅｌ", expected: ["Intel", "Ｉｎｔｅｌ"], label: "fullwidth Latin whole-word search", resultText: "2 of 2 conversations" },
      { query: "\"英特尔 Intel\"", expected: ["英特尔 Intel"], label: "mixed CJK Latin whole-word search", resultText: "1 of 1 conversations" },
      { query: "\"foo bar\"", expected: ["foo\nbar"], label: "newline phrase search", resultText: "1 of 1 conversations" },
    ]) {
      await page.locator("#global-search").fill(sample.query);
      await page.waitForFunction((text) => document.querySelector(".results-meta")?.textContent?.includes(text), sample.resultText, { timeout: 20_000 });
      await page.getByRole("button", { name: /DOM Multiscript Search Conversation/ }).click();
      await waitForActiveHighlightVisible(page);
      await waitForHighlightsAllowed(page, sample.expected);
      await assertHighlightsAllowed(page, sample.expected, sample.label);
      await assertStableMessageViewport(page, sample.label);
    }
    await page.getByLabel("Whole word").uncheck();
    await page.locator("#global-search").fill("");
    await page.unroute("**/api/conversations**");
    await waitForCount(page, ".conversation-item", 20);

    await page.getByLabel("Sort").selectOption("newest");
    await page.locator("#global-search").fill("英特尔");
    await page.waitForFunction(() => document.querySelector(".results-meta")?.textContent?.includes("6 of 6 conversations"), undefined, { timeout: 20_000 });
    const internalResultText = await page.getByRole("button", { name: /DOM Chinese Internal Hidden Result/ }).textContent();
    assert.ok(internalResultText?.includes("internal"), "sidebar should label internal-only search result context");
    await page.getByRole("button", { name: /DOM Chinese Internal Hidden Result/ }).click();
    await page.waitForFunction(() => document.querySelector(".reader-header h1")?.textContent?.includes("DOM Chinese Internal Hidden Result"), undefined, { timeout: 20_000 });
    await page.waitForFunction(() => document.querySelector(".message-scroll")?.textContent?.includes("Visible response without the hidden search token."), undefined, { timeout: 20_000 });
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Hidden hits"), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".message-scroll .search-highlight:visible").count(), 0, "hidden internal hit should not expose a visible reader mark before internal messages are shown");
    await assertStableMessageViewport(page, "hidden internal search result prompt");
    await page.getByRole("button", { name: "Show internal messages" }).first().click();
    await waitForActiveHighlightVisible(page);
    const activeChineseHighlight = await page.locator(".search-highlight-active").first().textContent();
    assert.equal(activeChineseHighlight, "英特尔", "active highlight range must align with JS UTF-16 slicing after emoji");
    await assertHighlightsEqual(page, "英特尔", "internal Chinese emoji search");
    await assertStableMessageViewport(page, "hidden internal search result after reveal");
    await page.getByLabel("Show internal messages").uncheck();
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Hidden hits"), undefined, { timeout: 20_000 });
    await assertStableMessageViewport(page, "hidden internal search result after hiding again");
    await page.getByRole("button", { name: /DOM Chinese Search Geometry Conversation/ }).click();
    await page.waitForFunction(() => document.querySelectorAll(".search-highlight").length >= 3, undefined, { timeout: 20_000 });
    await assertStableMessageViewport(page, "Chinese search result initial geometry");
    for (let idx = 0; idx < 3; idx += 1) {
      await page.getByRole("button", { name: "Next hit" }).click();
      await waitForActiveHighlightVisible(page);
      await assertStableMessageViewport(page, `Chinese search next hit ${idx}`);
    }
    await page.getByLabel("Show internal messages").check();
    await assertStableMessageViewport(page, "Chinese search with internal messages enabled");
    await page.getByLabel("Show internal messages").uncheck();
    await assertStableMessageViewport(page, "Chinese search after internal messages disabled");
    await page.getByRole("button", { name: /DOM Chinese Search Geometry Result 1/ }).click();
    await waitForActiveHighlightVisible(page);
    await assertStableMessageViewport(page, "Chinese search alternate result geometry");
    await page.reload({ waitUntil: "networkidle" });
    await page.locator("#global-search").fill("英特尔");
    await page.getByRole("button", { name: /DOM Chinese Search Geometry Conversation/ }).click();
    await waitForActiveHighlightVisible(page);
    await assertStableMessageViewport(page, "Chinese search geometry after reload");
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
      await page.waitForFunction(() => document.querySelector(".message-page-meta")?.textContent?.includes("of 380 visible messages"), undefined, { timeout: 20_000 });
    } catch (error) {
      const apiMessages = await (await fetch(new URL("/api/conversations/dom-long/messages?limit=5", baseUrl))).json();
      const readerText = await page.locator(".reader").textContent({ timeout: 1000 }).catch(() => "");
      throw new Error(`long conversation messages did not render; api_count=${apiMessages.items?.length ?? 0} total=${apiMessages.total ?? "unknown"} reader=${JSON.stringify((readerText || "").slice(0, 160))} diagnostics=${browserDiagnostics.join(" | ")}`);
    }
    const assistantBubble = await page.locator(".message-row-assistant .message").first().boundingBox();
    const userBubble = await page.locator(".message-row-user .message").first().boundingBox();
    assert.ok(assistantBubble && userBubble && userBubble.x > assistantBubble.x, "chat layout should align user messages to the right of assistant messages");

    await page.goto(`${baseUrl}?conversation=dom-long&layout=classic`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    assert.equal(await page.locator(".message-row-chat").count(), 0, "classic layout query parameter should restore row-by-row message blocks");
    await page.goto(`${baseUrl}?conversation=dom-long`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByLabel("Message layout").selectOption("classic");
    await page.waitForFunction(() => document.querySelectorAll(".message-row-chat").length === 0, undefined, { timeout: 20_000 });
    await page.getByLabel("Message layout").selectOption("chat");
    await page.waitForFunction(() => document.querySelector(".message-row-chat"), undefined, { timeout: 20_000 });
    await page.getByLabel("Density").selectOption("compact");
    await page.getByLabel("Font size").fill("16");
    await page.getByLabel("Message max width").fill("760");
    await page.getByRole("button", { name: "Close" }).click();
    await assertStableMessageViewport(page, "settings layout density font and width changes");
    const messageMetrics = await page.locator(".message-scroll").evaluate((node) => ({
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      before: node.scrollTop,
    }));
    assert.ok(messageMetrics.scrollHeight > messageMetrics.clientHeight, "message list must scroll internally");
    await page.locator(".message-scroll").evaluate((node) => { node.scrollTop = 500; });
    await page.waitForFunction(() => document.querySelector(".message-scroll")?.scrollTop > 0);

    await page.getByRole("button", { name: "Copy full conversation" }).click();
    await page.waitForFunction(() => window.__copiedText?.includes("Synthetic message 379"), undefined, { timeout: 20_000 });
    assert.equal(await page.evaluate(() => window.__copiedText.includes("Synthetic system context for DOM test")), true, "copy full conversation should include all nodes, including internal messages");
    await page.evaluate(() => { window.__copiedText = ""; });
    const slowCopyFirstPage = async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/conversations/dom-long/messages" && url.searchParams.get("offset") === "0") {
        await new Promise((resolve) => setTimeout(resolve, 300));
      }
      await route.continue();
    };
    await page.route("**/api/conversations/dom-long/messages**", slowCopyFirstPage, { times: 1 });
    await page.getByRole("button", { name: "Copy visible" }).click();
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Preparing copy"), undefined, { timeout: 20_000 });
    assert.equal(await page.getByRole("button", { name: "Preparing copy..." }).isDisabled(), true, "copy buttons should be disabled while preparing the full visible collection");
    await page.waitForFunction(() => window.__copiedText?.includes("Synthetic message 379"), undefined, { timeout: 20_000 });
    assert.equal(await page.evaluate(() => window.__copiedText.includes("Synthetic system context for DOM test")), false, "copy visible should copy the full current reader-visible message set, not only rendered rows");
    await page.unroute("**/api/conversations/dom-long/messages**", slowCopyFirstPage).catch(() => undefined);
    const copiedBeforeFailure = await page.evaluate(() => window.__copiedText || "");
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    });
    await page.getByRole("button", { name: "Copy visible" }).click();
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Copy failed"), undefined, { timeout: 20_000 });
    assert.equal(await page.evaluate(() => window.__copiedText || ""), copiedBeforeFailure, "missing Clipboard API must not be reported as a successful copy");
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: async (text) => { window.__copiedText = text; } },
      });
    });
    const failSecondMessagePage = async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/conversations/dom-long/messages" && url.searchParams.get("offset") === "300") {
        await route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"synthetic copy failure"}' });
        return;
      }
      await route.continue();
    };
    await page.route("**/api/conversations/dom-long/messages**", failSecondMessagePage);
    await page.getByRole("button", { name: "Copy full conversation" }).click();
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Copy conversation failed"), undefined, { timeout: 20_000 });
    assert.equal(await page.evaluate(() => window.__copiedText || ""), copiedBeforeFailure, "failed full-copy pagination must not overwrite clipboard with a partial conversation");
    await page.unroute("**/api/conversations/dom-long/messages**", failSecondMessagePage);

    await page.getByRole("button", { name: "Load more messages" }).click();
    await page.waitForFunction(() => document.querySelector(".message-page-meta")?.textContent?.includes("of 380 visible messages") && !document.querySelector(".message-page-meta button"), undefined, { timeout: 20_000 });

    const showRawCount = await page.getByRole("button", { name: "Show raw preview" }).count();
    assert.ok(showRawCount > 0, "raw preview toggle should be available");
    await page.getByRole("button", { name: "Show raw preview" }).first().click();
    await page.locator(".raw-message").first().waitFor({ state: "visible", timeout: 10_000 });
    await assertStableMessageViewport(page, "raw preview expansion");
    await page.getByRole("button", { name: "Open full raw JSON" }).first().click();
    await page.locator(".raw-full").first().waitFor({ state: "visible", timeout: 20_000 });
    await assertStableMessageViewport(page, "async full raw JSON expansion");
    await page.route("**/api/conversations/*/messages/*/raw", async (route) => {
      await route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"synthetic raw failure"}' });
    }, { times: 1 });
    await page.getByRole("button", { name: "Close full raw JSON" }).first().click();
    await page.waitForFunction(() => document.querySelectorAll(".raw-full").length === 0, undefined, { timeout: 20_000 });
    await assertStableMessageViewport(page, "full raw JSON collapse");
    await page.getByRole("button", { name: "Open full raw JSON" }).first().click();
    await page.locator(".raw-error").first().waitFor({ state: "visible", timeout: 20_000 });
    await assertStableMessageViewport(page, "full raw JSON error state");
    await page.unroute("**/api/conversations/*/messages/*/raw");
    await page.getByRole("button", { name: "Open full raw JSON" }).first().click();
    await page.locator(".raw-full").first().waitFor({ state: "visible", timeout: 20_000 });
    assert.equal(await page.locator(".raw-error").count(), 0, "full raw retry should clear the visible error state");
    await assertStableMessageViewport(page, "full raw JSON retry success");
    await page.getByRole("button", { name: "Close full raw JSON" }).first().click();
    await page.waitForFunction(() => document.querySelectorAll(".raw-full").length === 0, undefined, { timeout: 20_000 });
    await page.getByRole("button", { name: "Hide raw preview" }).first().click();
    await page.waitForFunction(() => document.querySelectorAll(".raw-message").length === 0, undefined, { timeout: 20_000 });
    await assertStableMessageViewport(page, "raw preview collapse");

    await page.locator(".message-scroll").evaluate((node) => { node.scrollTop = 0; });
    await page.getByLabel("Show internal messages").check();
    await page.locator(".message-disclosure.message-internal").first().waitFor({ state: "visible", timeout: 20_000 });
    await page.getByRole("button", { name: "Copy full conversation" }).click();
    await page.waitForFunction(() => window.__copiedText?.includes("Synthetic system context for DOM test"), undefined, { timeout: 20_000 });
    assert.equal(await page.locator('[data-node-id="root"]').count(), 0, "empty root mapping node should not render as a normal visible message");
    assert.equal(await page.evaluate(() => window.__copiedText.includes("root:\n") || window.__copiedText.includes("message:\n\n")), false, "copy conversation should skip empty mapping nodes");
    assert.ok(await page.locator(".message-disclosure.message-internal").first().evaluate((node) => !node.open), "internal messages should appear collapsed by default in chat layout");
    assert.equal(await page.locator(".message-disclosure.message-internal .raw-message").count(), 0, "closed internal details should not expose raw preview content");
    await page.locator(".message-scroll").evaluate((node) => { node.scrollTop = Math.floor(node.scrollHeight * 0.45); });
    await page.waitForTimeout(80);
    await assertStableMessageViewport(page, "middle scroll after showing internal messages");
    await page.locator(".message-scroll").evaluate((node) => { node.scrollTop = node.scrollHeight - node.clientHeight - 4; });
    await page.waitForTimeout(80);
    await assertStableMessageViewport(page, "bottom scroll after showing internal messages");
    await page.locator(".message-scroll").evaluate((node) => { node.scrollTop = 0; });
    const internalIndex = await page.locator(".message-disclosure.message-internal").evaluateAll((nodes) => {
      const index = nodes.findIndex((node) => node.textContent?.includes("Synthetic system context"));
      return index >= 0 ? index : Math.min(1, Math.max(0, nodes.length - 1));
    });
    await page.locator(".message-disclosure.message-internal summary").nth(internalIndex).click();
    const openInternal = page.locator(".message-disclosure.message-internal").nth(internalIndex);
    await openInternal.waitFor({ state: "visible", timeout: 20_000 });
    await assertStableMessageViewport(page, "expanded internal details");
    assert.equal(await openInternal.getByRole("button", { name: "Show internal" }).count(), 0, "opened internal details should not require a second internal reveal button");
    await page.waitForFunction(
      () => Array.from(document.querySelectorAll(".message-text")).some((node) => node.textContent?.includes("Synthetic system context")),
      undefined,
      { timeout: 20_000 },
    );
    if (await openInternal.getByRole("button", { name: "Show raw preview" }).count()) {
      await openInternal.getByRole("button", { name: "Show raw preview" }).click();
      await openInternal.locator(".raw-message").first().waitFor({ state: "visible", timeout: 10_000 });
      await assertStableMessageViewport(page, "internal raw preview expansion");
      await openInternal.getByRole("button", { name: "Open full raw JSON" }).click();
      await openInternal.locator(".raw-full").first().waitFor({ state: "visible", timeout: 20_000 });
      await assertStableMessageViewport(page, "internal full raw JSON expansion");
      await openInternal.getByRole("button", { name: "Close full raw JSON" }).click();
      await openInternal.locator(".raw-full").waitFor({ state: "hidden", timeout: 20_000 });
      await openInternal.getByRole("button", { name: "Hide raw preview" }).click();
      await openInternal.locator(".raw-message").waitFor({ state: "hidden", timeout: 20_000 });
      await assertStableMessageViewport(page, "internal raw preview collapse");
    }
    await openInternal.locator("summary").click();
    await page.waitForFunction(
      (index) => !document.querySelectorAll(".message-disclosure.message-internal")[index]?.open,
      internalIndex,
      { timeout: 20_000 },
    );
    assert.equal(await page.locator(".message-disclosure.message-internal .raw-message:visible").count(), 0, "closed internal details should hide raw preview and full raw");
    await assertStableMessageViewport(page, "collapsed internal details");

    for (let idx = 0; idx < 3; idx += 1) {
      await page.getByLabel("Show internal messages").uncheck();
      await assertStableMessageViewport(page, `rapid internal toggle off ${idx}`);
      await page.getByLabel("Show internal messages").check();
      await page.locator(".message-disclosure.message-internal").first().waitFor({ state: "visible", timeout: 20_000 });
      await assertStableMessageViewport(page, `rapid internal toggle on ${idx}`);
    }

    await page.locator("#global-search").fill("Synthetic system context");
    await page.waitForTimeout(300);
    await assertStableMessageViewport(page, "active hit in folded internal message");

    await page.locator("#global-search").fill("sqlite3");
    await page.waitForFunction(() => document.querySelectorAll(".search-highlight").length > 0, undefined, { timeout: 20_000 });
    await assertStableMessageViewport(page, "search result initial selection with highlights");
    const sqliteResultCount = await page.locator(".conversation-item").count();
    for (let idx = 0; idx < Math.min(3, sqliteResultCount); idx += 1) {
      await page.locator(".conversation-item").nth(idx).click();
      await page.waitForFunction(() => document.querySelectorAll(".search-highlight").length > 0, undefined, { timeout: 20_000 });
      await assertStableMessageViewport(page, `search result selection ${idx} with highlights`);
      await page.getByRole("button", { name: "Next hit" }).click();
      await waitForActiveHighlightVisible(page);
      await assertStableMessageViewport(page, `search next hit ${idx}`);
      await page.getByRole("button", { name: "Prev hit" }).click();
      await waitForActiveHighlightVisible(page);
      await assertStableMessageViewport(page, `search previous hit ${idx}`);
    }

    await page.locator("#global-search").fill("a/b");
    await page.waitForFunction(() => document.querySelector("#global-search")?.value === "a/b", undefined, { timeout: 10_000 });

    await page.goto(`${baseUrl}?conversation=dom-role-class`, { waitUntil: "networkidle" });
    await page.getByLabel("Show internal messages").check();
    await page.waitForFunction(() => document.querySelector(".message-role-tool-system"), undefined, { timeout: 20_000 });
    const toolClassName = await page.locator(".message-role-tool-system").first().evaluate((node) => node.className);
    assert.ok(!toolClassName.includes("/"), "message role classes must be CSS-safe");

    await page.goto(`${baseUrl}?conversation=dom-tech-json`, { waitUntil: "networkidle" });
    await page.locator(".message-disclosure.message-role-assistant").first().waitFor({ state: "visible", timeout: 20_000 });
    assert.ok(await page.locator(".message-row-user .message-disclosure").count() === 0, "ordinary user JSON should not be folded as technical payload");
    assert.ok(await page.locator(".message-row-assistant .message-disclosure").count() === 0, "ordinary assistant code block should not be folded as technical payload");
    assert.ok(await page.locator(".message-row-assistant .message-text").filter({ hasText: "ordinary JSON result" }).count() >= 1, "ordinary assistant JSON with a query key should stay expanded");
    assert.ok(await page.locator(".message-disclosure.message-role-assistant").count() >= 1, "assistant tool JSON should fold as a technical payload");
    await page.getByLabel("Show internal messages").check();
    await page.locator(".message-disclosure.message-internal").first().waitFor({ state: "visible", timeout: 20_000 });
    assert.ok(await page.locator(".message-disclosure.message-role-assistant").count() >= 2, "source analysis assistant payload should be hidden until internal messages are shown, then fold as technical");
    assert.equal(await page.locator(".message-scroll").evaluate((node) => node.scrollWidth > node.clientWidth), false, "technical JSON should not cause horizontal message scrolling");

    await page.goto(`${baseUrl}?conversation=dom-title-only`, { waitUntil: "networkidle" });
    await page.locator("details.advanced-panel").evaluate((node) => { node.open = true; });
    await page.getByLabel("Scope").selectOption("title");
    await page.getByLabel("Title contains").fill("title-only-target");
    await page.waitForFunction(() => document.querySelector(".reader-header h1")?.textContent?.includes("title-only-target"), undefined, { timeout: 20_000 });
    const titleResultText = await page.getByRole("button", { name: /title-only-target synthetic conversation/ }).textContent();
    assert.ok(titleResultText?.includes("title"), "sidebar should label title-only search result context");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Title match"), undefined, { timeout: 20_000 });
    await page.waitForFunction(() => Array.from(document.querySelectorAll(".search-visibility-notes span")).some((node) => node.textContent?.includes("conversation title")), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".search-highlight").count(), 0);

    await page.goto(`${baseUrl}?conversation=dom-branch-override`, { waitUntil: "networkidle" });
    await page.locator("#global-search").fill("PATH:ALL branchoverride-token");
    await page.waitForFunction(() => document.querySelector(".reader-header h1")?.textContent?.includes("DOM Branch Override Conversation"), undefined, { timeout: 20_000 });
    await page.waitForFunction(() => document.querySelector(".search-visibility-notes")?.textContent?.includes("Query overrides path"), undefined, { timeout: 20_000 });
    await page.waitForFunction(() => document.querySelector(".message-scroll")?.textContent?.includes("branchoverride-token"), undefined, { timeout: 20_000 });
    assert.equal(await page.getByLabel("Message path").inputValue(), "all", "reader path dropdown should show effective query path override");
    assert.equal(await page.getByLabel("Message path").isDisabled(), true, "reader path dropdown should be disabled while raw query overrides it");
    assert.equal(await page.getByLabel("Search path").isDisabled(), true, "sidebar path dropdown should be disabled while raw query overrides it");
    const overrideDownload = await page.getByRole("link", { name: "Download MD" }).getAttribute("href");
    assert.ok(overrideDownload?.includes("path=all"), "download link should use effective path override");
    assert.ok(overrideDownload?.includes("include_internal=false"), "download link should use current internal visibility");
    await waitForActiveHighlightVisible(page);

    await page.locator("#global-search").fill("SCOPE:TITLE Branch");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Title match"), undefined, { timeout: 20_000 });
    await page.waitForFunction(() => document.querySelector(".search-visibility-notes")?.textContent?.includes("Query overrides scope"), undefined, { timeout: 20_000 });
    await page.locator("details.advanced-panel").evaluate((node) => { node.open = true; });
    assert.equal(await page.getByLabel("Scope").isDisabled(), true, "scope dropdown should be disabled while raw query overrides it");
    assert.equal(await page.locator(".search-highlight").count(), 0, "scope:title raw override should not create body hit navigation");

    await page.goto(`${baseUrl}?conversation=dom-reader-filter`, { waitUntil: "networkidle" });
    await page.locator("details.advanced-panel").evaluate((node) => { node.open = true; });
    await page.getByLabel("Scope").selectOption("title");
    await page.waitForTimeout(250);
    assert.equal(await page.locator(".search-highlight").count(), 0, "scope alone should not activate reader search highlights");
    assert.equal(await page.locator(".hit-counter").textContent(), "", "scope alone should not activate reader hit navigation state");
    await page.getByLabel("Scope").selectOption("all");
    await page.getByLabel("Source shard").fill("conversations.json");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Filter match"), undefined, { timeout: 20_000 });
    await page.waitForFunction(() => Array.from(document.querySelectorAll(".search-visibility-notes span")).some((node) => node.textContent?.includes("matches the current filters")), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".search-highlight").count(), 0, "source-only filter should not create body highlights");
    await page.getByLabel("Source shard").fill("");
    await page.getByLabel("Exclude").fill("absent-filter-token");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Filter match"), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".search-highlight").count(), 0, "exclude-only filter should not create body highlights");
    await page.getByLabel("Exclude").fill("");
    await page.getByLabel("Role").selectOption("assistant");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Filter match"), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".search-highlight").count(), 0, "role-only filter should not be presented as a body hit");
    await page.getByLabel("Role").selectOption("");
    await page.locator("#global-search").fill("-filtertarget");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Filter match"), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".search-highlight").count(), 0, "raw exclude-only q should not create body hit UI");
    await page.locator("#global-search").fill("source:conversations.json");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Filter match"), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".search-highlight").count(), 0, "raw source-only q should not create body hit UI");
    await page.locator("#global-search").fill("role:assistant");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Filter match"), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".search-highlight").count(), 0, "raw role-only q should not create body hit UI");
    await page.locator("#global-search").fill("title:DOM");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Title match"), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".search-highlight").count(), 0, "raw title-only q should not create body hit UI");
    await page.locator("#global-search").fill("source:conversations.json filtertarget");
    await page.waitForFunction(() => document.querySelectorAll(".search-highlight").length >= 1, undefined, { timeout: 20_000 });
    assert.ok(await page.locator(".search-highlight").count() >= 1, "raw source plus body q should still highlight body term");
    await page.locator("#global-search").fill("");
    await page.getByLabel("Scope").selectOption("all");
    await page.getByLabel("Title contains").fill("");
    await page.getByLabel("Exclude").fill("excluded");
    await page.locator("#global-search").fill("filtertarget");
    await page.waitForFunction(() => document.querySelector(".reader-header h1")?.textContent?.includes("DOM Reader Filter Conversation"), undefined, { timeout: 20_000 });
    await page.waitForFunction(
      () => {
        const excluded = Array.from(document.querySelectorAll(".message")).find((node) => node.textContent?.includes("filtertarget excluded body"));
        return Boolean(excluded) && excluded.querySelectorAll(".search-highlight").length === 0 && document.querySelectorAll(".search-highlight").length >= 2;
      },
      undefined,
      { timeout: 20_000 },
    );
    assert.equal(await page.locator(".message", { hasText: "filtertarget excluded body" }).locator(".search-highlight").count(), 0, "excluded reader message should not be highlighted");
    await page.getByLabel("Exclude").fill("");
    await page.getByLabel("Role").selectOption("assistant");
    await page.waitForFunction(
      () => !document.querySelector(".message-role-user .search-highlight") && Boolean(document.querySelector(".message-role-assistant .search-highlight")),
      undefined,
      { timeout: 20_000 },
    );
    assert.equal(await page.locator(".message-role-user", { hasText: "filtertarget user body" }).locator(".search-highlight").count(), 0, "reader role filter should suppress user highlights");
    assert.ok(await page.locator(".message-role-assistant", { hasText: "filtertarget assistant body" }).locator(".search-highlight").count() >= 1, "reader role filter should keep assistant highlights");
    await assertStableMessageViewport(page, "reader filter-aware highlights");
    await page.getByLabel("Role").selectOption("");
    await page.getByLabel("Exact phrase").fill('foo "bar"');
    await page.waitForFunction(() => Array.from(document.querySelectorAll(".search-highlight")).some((node) => node.textContent === 'foo "bar"'), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".message", { hasText: "filtertarget user body" }).locator(".search-highlight").count(), 0, "exact filter should not behave like a manually quoted q fragment");
    await page.getByLabel("Exact phrase").fill("backslash \\ marker");
    await page.waitForFunction(() => Array.from(document.querySelectorAll(".search-highlight")).some((node) => node.textContent === "backslash \\ marker"), undefined, { timeout: 20_000 });
    await page.getByLabel("Exact phrase").fill("");
    await page.getByLabel("Exclude").fill("titleblock");
    await page.waitForFunction(() => document.querySelectorAll(".search-highlight").length === 0, undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".message", { hasText: "filtertarget assistant body" }).locator(".search-highlight").count(), 0, "title exclude should suppress reader highlights for retained selected conversation");
    await page.getByLabel("Exclude").fill("");

    await page.goto(`${baseUrl}?conversation=dom-tech-json`, { waitUntil: "networkidle" });
    await page.locator("details.advanced-panel").evaluate((node) => { node.open = true; });
    await page.getByLabel("Scope").selectOption("all");
    await page.locator("#global-search").fill("search_query");
    await page.waitForFunction(() => document.querySelector(".message-disclosure.message-role-assistant")?.open, undefined, { timeout: 20_000 });
    await waitForActiveHighlightVisible(page);
    await assertStableMessageViewport(page, "technical payload active hit opens details");

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
    await page.goto(`${baseUrl}?conversation=dom-role-class`, { waitUntil: "networkidle" });
    await page.getByLabel("显示内部消息").check();
    await page.waitForFunction(() => Array.from(document.querySelectorAll(".role-pill")).some((node) => node.textContent?.includes("工具/系统")), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".role-pill").filter({ hasText: "tool/system" }).count(), 0, "localized UI should not expose raw role enum labels");

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
