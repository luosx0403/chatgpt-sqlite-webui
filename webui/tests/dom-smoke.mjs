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
  const result = spawnSync(candidate.command, [...candidate.args, "-c", "import fastapi, uvicorn"], {
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
  const messageBlockSource = fs.readFileSync(path.join(webRoot, "src/components/MessageBlock.tsx"), "utf8");
  const querySyntaxSource = fs.readFileSync(path.join(webRoot, "src/utils/querySyntax.ts"), "utf8");
  const i18nSource = fs.readFileSync(path.join(webRoot, "src/i18n.ts"), "utf8");
  const stylesSource = fs.readFileSync(path.join(webRoot, "src/styles.css"), "utf8");
  const interactionSource = fs.readFileSync(path.join(webRoot, "src/utils/interaction.ts"), "utf8");
  const buildSource = fs.readFileSync(path.join(webRoot, "scripts/build.mjs"), "utf8");
  assert.ok(appSource.includes('web_index_recovery: t("stageWebIndexRecovery")'), "web-index-recovery import stage should use a localized label");
  assert.ok(appSource.includes('data-testid="web-index-progress"'), "Web index processed/total progress should be visible without exposing internal field names");
  assert.ok(appSource.includes('data-testid="web-index-cancel-button"'), "active Web index builds should expose a cancellable import-job control");
  assert.ok(clientSource.includes("/web-index/cancel"), "Web index cancellation should use the dedicated bounded endpoint");
  assert.ok(appSource.includes('scan_normalize_messages: t("webIndexStageScanMessages")'), "Web index build stages should use localized labels");
  assert.ok(appSource.includes("has_internal_hits: meta.has_internal_hits"), "selected conversation merge must preserve hidden/internal search metadata after detail load");
  assert.ok(appSource.includes("void has_internal_hits"), "selected conversation metadata clear must remove stale internal search metadata");
  assert.ok(clientSource.includes("count_total"), "message hit client should expose count_total for fast navigation requests");
  assert.ok(clientSource.includes("getConversationCopyText"), "full conversation copy should use the dedicated server-side text stream");
  assert.ok(clientSource.includes("getVisibleMessagesCopyText"), "visible copy should use the dedicated selected-row stream");
  assert.ok(clientSource.includes("response.body.getReader()"), "full conversation copy should consume a ReadableStream instead of response.text()");
  assert.equal(clientSource.includes("return response.text()"), false, "full conversation copy must not allocate an unbounded response string");
  assert.ok(clientSource.includes("MAX_BROWSER_COPY_BYTES"), "browser copy must enforce a byte budget");
  assert.ok(clientSource.includes("MAX_BROWSER_COPY_CHARS"), "browser copy must enforce a character budget");
  assert.ok(clientSource.includes("reader.cancel()"), "over-limit or failed stream copy should cancel the response reader");
  assert.ok(clientSource.includes("structured.cleanup_warnings"), "ApiError should parse every structured cleanup warning");
  assert.ok(appSource.includes("cleanupWarningLabel"), "cleanup warning codes and path kinds should render as localized safe text");
  assert.ok(appSource.includes('return t("importError_json_resource_limits")'), "JSON element resource failures must not be mislabeled as ZIP upload limits");
  assert.ok(i18nSource.includes("importError_json_resource_limits"), "JSON element resource failures should have localized user-facing text");
  assert.ok(paneSource.includes("getConversationCopyText"), "reader full-copy must not accumulate reader page objects");
  assert.ok(paneSource.includes("countTotal: false"), "reader hit navigation should request fast message-hit pages without exact total counts");
  assert.equal(paneSource.includes("while (items.length < MAX_NAVIGABLE_HIT_MESSAGES)"), false, "reader hit navigation must not serially prefetch ten pages on initial load");
  assert.ok(paneSource.includes("HIT_PREFETCH_THRESHOLD"), "reader hit navigation should lazily append near the loaded boundary");
  assert.ok(messageBlockSource.includes("getMessageDisplayChunk"), "truncated reader messages should have an explicit bounded expansion path");
  assert.ok(messageBlockSource.includes("chunk.resolver_input_truncated || (!chunk.has_more && !chunk.total_chars_exact)"), "single-message expansion/copy must distinguish normal intermediate chunks from terminal incomplete raw recovery");
  assert.ok(paneSource.includes("getVisibleMessagesCopyText"), "Copy visible must bind loaded message IDs to one server snapshot");
  assert.ok(paneSource.includes("MAX_VISIBLE_COPY_SELECTION"), "visible copy must cap the server selection contract locally");
  assert.ok(paneSource.includes("nodeIds.length > MAX_VISIBLE_COPY_SELECTION"), "an oversized visible selection must be rejected before the request");
  assert.ok(messageBlockSource.includes("message.display_text_resolver_input_truncated"), "initial reader metadata must expose resolver incompleteness separately from unknown total length");
  assert.ok(i18nSource.includes("displayRecoveryIncomplete"), "incomplete raw recovery should have an accessible localized warning");
  assert.ok(messageBlockSource.includes("JSON.stringify([conversationId, message.node_id"), "message state keys should use collision-free tuple serialization");
  assert.ok(messageBlockSource.includes('hasOwnProperty.call(savedState, "displayNextOffset")'), "terminal next_offset=null must survive remount");
  assert.ok(clientSource.includes("new CopyLimitError()"), "visible/full copy streams must stop at the browser copy budget");
  assert.ok(paneSource.indexOf("assertBrowserCopyLimit(text)") < paneSource.indexOf("navigator.clipboard.writeText(text)"), "copy limits must be checked before clipboard mutation");
  assert.ok(paneSource.includes("readerDataContextRef.current !== expectedContextKey || requestId !== copyRequestRef.current"), "clipboard mutation must be guarded by the current reader context");
  assert.ok(messageBlockSource.includes("[messageIdentity, showRawDefault]"), "message content state should reset only for data identity/default changes");
  assert.equal(messageBlockSource.includes("[messageIdentity, showRawDefault, layout]"), false, "pure layout changes must preserve message content state");
  assert.ok(paneSource.includes("readerDataContextKey"), "reader requests should use a data-only context key");
  assert.ok(paneSource.includes("readerLayoutContextKey"), "reader visual remeasure should use a separate layout context key");
  assert.ok(appSource.includes("canonicalShareUrl"), "Copy URL should use a canonical serializer");
  assert.ok(
    appSource.includes("loadConversationPage(listContinuationRef.current ? 0 : (nextOffset ?? 0), true)"),
    "conversation continuation must preserve the token-bound initial offset",
  );
  for (const explicitParam of ['params.set("match_mode"', 'params.set("layout"', 'params.set("show_internal"']) {
    assert.ok(appSource.includes(explicitParam), `canonical Copy URL should explicitly include ${explicitParam}`);
  }
  assert.equal(appSource.includes("focusIndex"), false, "unreachable sidebar navigation state should be removed");
  assert.ok(appSource.includes("appliedShareStateRef"), "Copy URL should serialize one accepted search/list/selection context");
  for (const selector of ["button", "a[href]", "summary", "[role='button']", "[role='menuitem']", "[tabindex]:not([tabindex='-1'])"]) {
    assert.ok(interactionSource.includes(selector), `interactive target helper should recognize ${selector}`);
  }
  assert.ok(interactionSource.includes("target.closest"), "interactive target helper should recognize nested icons/spans through closest()");
  assert.ok(paneSource.includes("visible_total"), "reader should consume visible message totals from the API");
  assert.ok(paneSource.includes("effectivePath"), "reader download/copy/navigation should use effective query path");
  assert.ok(clientSource.includes("include_internal"), "reader download links should pass current internal visibility");
  assert.ok(paneSource.includes('disabled={Boolean(querySyntax.pathOverride)}'), "overridden path select should not look interactive");
  assert.ok(querySyntaxSource.includes("toLowerCase"), "frontend query syntax should use locale-independent modifier case folding");
  assert.equal(querySyntaxSource.includes("toLocaleLowerCase"), false, "modifier parsing must not depend on the browser locale");
  assert.ok(querySyntaxSource.includes("readQuoted"), "frontend query syntax should parse quoted modifier values");
  assert.ok(i18nSource.includes("stageWebIndexRecovery"), "web-index-recovery stage label should be translated");
  assert.ok(i18nSource.includes("UTC calendar days"), "date filter UTC wording should be visible in search help");
  assert.ok(i18nSource.includes("preparingCopy"), "copy loading state should be localized");
  assert.ok(appSource.includes("importInputRef"), "successful upload should be able to clear the file input");
  assert.ok(appSource.includes('data-testid="import-zip-button"'), "ZIP import should expose a visible keyboard-focusable button");
  assert.ok(i18nSource.includes('PARTIAL_LANGUAGES = ["ja", "es"]'), "partial Japanese and Spanish coverage should be explicit");
  assert.ok(buildSource.includes("failBeforeIndex"), "dist publication should exercise an injected pre-index failure");
  assert.ok(buildSource.indexOf("for (const relative of newFiles)") < buildSource.indexOf('path.join(dist, "index.html")'), "dist assets must publish before the atomic index entry point");
  assert.ok(stylesSource.includes(".search-diagnostics-hint"), "diagnostics hint styles should target the current class");
  assert.equal(stylesSource.includes(".diagnostics-hint"), false, "stale diagnostics-hint selector should not return");
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

  const longBodyMapping = {
    root: root(["long-body"]),
    "long-body": node("long-body", "root", "assistant", "Synthetic long body placeholder.", 1_950_100_001),
  };
  conversations.push(conversation("dom-long-body", "DOM Long Body Conversation", longBodyMapping, "long-body", 1_950_100_000));

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

  const damagedCurrentMapping = {
    root: root(["u"]),
    u: node("u", "root", "user", "damaged-current-visible-needle is visible through current fallback.", 1_955_300_001, ["a"]),
    a: node("a", "u", "assistant", "Assistant repeats damaged-current-visible-needle for navigation.", 1_955_300_002),
  };
  conversations.push(conversation("dom-damaged-current", "DOM Damaged Current Fallback", damagedCurrentMapping, "a", 1_955_300_000));

  const readerFilterMapping = {
    root: root(["u"]),
    u: node("u", "root", "user", "filtertarget user body", 1_955_500_001, ["a"]),
    a: node("a", "u", "assistant", "filtertarget assistant body", 1_955_500_002, ["b"]),
    b: node("b", "a", "assistant", "filtertarget excluded body", 1_955_500_003, ["c"]),
    c: node("c", "b", "assistant", 'filtertarget exact phrase foo "bar" and backslash \\ marker amber birch cedar denim ember frost glade hazel ivory jewel khaki', 1_955_500_004),
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
    b: node("b", "q", "assistant", '{"search_query":[{"q":"ordinary assistant JSON example"}],"response_length":"short"}', 1_957_000_004, ["d"]),
    d: node("d", "b", "tool/system", '{"search_query":[{"q":"synthetic docs"}],"response_length":"short"}', 1_957_000_005, ["c"]),
    c: rawNode("c", "d", "assistant", { content_type: "thoughts", text: "source analysis msg id: synthetic-source-analysis-id", extra: "x".repeat(1200) }, 1_957_000_006),
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
    a: node("a", "u", "assistant", "Intelligence and Intellicode are longer words for whole-word negative checks.", 1_958_206_002, ["late"]),
    late: node(
      "late",
      "a",
      "assistant",
      `${"😀".repeat(9_000)}ASTRAL-LATE-NEEDLE cafe\u0301 ﬁ`,
      1_958_206_003,
    ),
  };
  conversations.push(conversation("dom-multiscript", "DOM Multiscript Search Conversation", multiscriptMapping, "late", 1_958_206_000));
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
  const fallbackId = "fallback/id?hash%:漢字";
  const fallbackMapping = {
    root: root(["u"]),
    u: node("u", "root", "user", "<script>window.__fallbackInjected=true</script> safe text", 2_100_000_001),
  };
  conversations.push(conversation(fallbackId, "<img src=x onerror=window.__fallbackInjected=true>", fallbackMapping, "u", 2_100_000_000));
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
  let fallbackServer;
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
    let noDbServerError = "";
    noDbServer.stdout.on("data", () => undefined);
    noDbServer.stderr.on("data", (chunk) => { noDbServerError = `${noDbServerError}${chunk}`.slice(-4000); });
    try {
      await waitForHealth(noDbUrl);
    } catch (error) {
      throw new Error(`${error instanceof Error ? error.message : String(error)}; server=${noDbServerError.trim() || `exit ${noDbServer.exitCode}`}`);
    }
    const noDbContext = await browser.newContext({ viewport: { width: 1100, height: 760 }, locale: "en-US" });
    const noDbPage = await noDbContext.newPage();
    await noDbPage.goto(noDbUrl, { waitUntil: "networkidle" });
    assert.equal(await noDbPage.locator("text=Fallback UI").count(), 0, "no-db web should serve React UI, not fallback");
    await noDbPage.getByTestId("import-panel").waitFor({ state: "visible", timeout: 20_000 });
    let failedUploadOnce = false;
    await noDbPage.route("**/api/import/upload", async (route) => {
      if (!failedUploadOnce) {
        failedUploadOnce = true;
        await route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({
            detail: {
              code: "upload_preflight_failed",
              cleanup_warning: "upload_file_unlink_failed",
              cleanup_error_type: "PermissionError",
              cleanup_warnings: [
                { code: "upload_file_unlink_failed", error_type: "PermissionError", path_kind: "upload_file" },
                { code: "upload_directory_cleanup_failed", error_type: "PermissionError", path_kind: "upload_directory" },
              ],
            },
          }),
        });
        return;
      }
      await route.continue();
    });
    await noDbPage.getByTestId("import-zip-input").setInputFiles(uploadZip);
    await noDbPage.getByTestId("import-start-button").click();
    await noDbPage.waitForFunction(() => document.querySelector('[data-testid="import-panel"]')?.textContent?.includes("could not be prepared safely"), undefined, { timeout: 20_000 });
    assert.equal((await noDbPage.getByTestId("import-panel").textContent())?.includes("synthetic upload failure"), false, "upload API details must not leak into localized UI");
    const preflightWarnings = await noDbPage.getByTestId("preflight-cleanup-warnings").textContent();
    assert.ok(preflightWarnings?.includes("temporary uploaded file"), "preflight cleanup should show its localized file warning");
    assert.ok(preflightWarnings?.includes("temporary upload directory"), "preflight cleanup should show every localized warning");
    assert.equal(preflightWarnings?.includes("upload_file_unlink_failed"), false, "cleanup UI must not expose internal warning enum names");
    assert.equal(preflightWarnings?.includes("PermissionError"), false, "cleanup UI must not expose OS error class details");
    assert.ok((await noDbPage.getByTestId("import-panel").textContent())?.includes("Selected ZIP"), "failed upload should keep selected file for retry");
    assert.equal(await noDbPage.getByTestId("import-start-button").count(), 1, "failed upload should keep retry button");
    await noDbPage.waitForFunction(() => {
      const button = document.querySelector('[data-testid="import-start-button"]');
      return button instanceof HTMLButtonElement && !button.disabled;
    }, undefined, { timeout: 20_000 });
    let activeJobPolls = 0;
    let maxActiveJobPolls = 0;
    let delayedFirstJobPoll = true;
    let allowTerminalJobPoll = false;
    let webIndexCancelCalls = 0;
    await noDbPage.route("**/api/import/jobs/**", async (route) => {
      const requestUrl = new URL(route.request().url());
      if (route.request().method() === "POST" && requestUrl.pathname.endsWith("/web-index/cancel")) {
        webIndexCancelCalls += 1;
        const jobId = requestUrl.pathname.split("/").at(-3);
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            job_id: jobId,
            status: "running",
            stage: "web-index",
            outcome: "canonical_commit_succeeded",
            completion_outcome: "running",
            canonical_import_outcome: "running",
            canonical_commit_succeeded: true,
            elapsed_seconds: 1.8,
            web_index_cancel_requested: true,
            web_index_cancelled: false,
            web_index: { status: "cancelling", processed: 100, total: 250, complete: false },
          }),
        });
        return;
      }
      activeJobPolls += 1;
      maxActiveJobPolls = Math.max(maxActiveJobPolls, activeJobPolls);
      try {
        if (delayedFirstJobPoll || !allowTerminalJobPoll) {
          if (delayedFirstJobPoll) {
            delayedFirstJobPoll = false;
            await new Promise((resolve) => setTimeout(resolve, 1_600));
          }
          const jobId = new URL(route.request().url()).pathname.split("/").pop();
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              job_id: jobId,
              status: "running",
              stage: "web-index",
              outcome: "canonical_commit_succeeded",
              completion_outcome: "running",
              canonical_import_outcome: "running",
              canonical_commit_succeeded: true,
              elapsed_seconds: 1.6,
              web_index: {
                status: "building",
                build_stage: "scan_normalize_messages",
                processed: 100,
                total: 250,
                complete: false,
                batch_size: 100,
              },
            }),
          });
        } else {
          const jobId = new URL(route.request().url()).pathname.split("/").pop();
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              job_id: jobId,
              status: "succeeded",
              stage: "succeeded",
              outcome: "succeeded",
              completion_outcome: "cleanup_warning",
              canonical_import_outcome: "partial_success",
              canonical_commit_succeeded: true,
              elapsed_seconds: 3.0,
              summary: {
                committed_conversations: 2,
                committed_nodes: 7,
                skipped_invalid_elements: 1,
                warnings: 1,
                warnings_by_type: [
                  { warning_type: "conversation_node_limit_exceeded", count: 1 },
                ],
              },
              cleanup_warning: "summary_update_after_commit_failed",
              cleanup_warnings: [
                { code: "summary_update_after_commit_failed", error_type: "OperationalError", path_kind: "import_summary" },
                { code: "import_connection_close_failed", error_type: "OperationalError", path_kind: "database_connection" },
              ],
            }),
          });
        }
      } finally {
        activeJobPolls -= 1;
      }
    });
    await noDbPage.getByTestId("import-start-button").click();
    try {
      await noDbPage.getByTestId("web-index-progress").waitFor({ state: "visible", timeout: 20_000 });
    } catch (error) {
      const panelText = await noDbPage.getByTestId("import-panel").textContent().catch(() => "");
      throw new Error(`web-index progress did not render; active=${activeJobPolls} max=${maxActiveJobPolls} panel=${JSON.stringify(panelText)} cause=${error instanceof Error ? error.message : String(error)}`);
    }
    assert.ok((await noDbPage.getByTestId("web-index-progress").textContent())?.includes("Normalizing messages · 100/250"), "Web index progress should use a localized stage label and bounded counts");
    const cancelResponse = noDbPage.waitForResponse((response) => (
      response.request().method() === "POST"
      && new URL(response.url()).pathname.endsWith("/web-index/cancel")
    ));
    await noDbPage.getByTestId("web-index-cancel-button").click();
    await noDbPage.waitForFunction(() => document.querySelector('[data-testid="web-index-cancel-button"]')?.textContent?.includes("Cancelling"), undefined, { timeout: 20_000 });
    await cancelResponse;
    assert.equal(webIndexCancelCalls, 1, "Web index cancellation should issue exactly one POST request");
    assert.equal(await noDbPage.getByTestId("web-index-cancel-button").isDisabled(), true, "the cancellation button should disable after acknowledgement");
    allowTerminalJobPoll = true;
    await noDbPage.waitForFunction(() => document.querySelector('[data-testid="import-status"]')?.textContent?.includes("succeeded"), undefined, { timeout: 60_000 });
    await noDbPage.waitForTimeout(1_500);
    assert.equal(maxActiveJobPolls, 1, "import job polling must be serial even when one response exceeds the polling interval");
    assert.ok((await noDbPage.getByTestId("import-status").textContent())?.includes("succeeded"), "a late running response must not regress a terminal import status");
    const importSummary = await noDbPage.getByTestId("import-summary").textContent();
    assert.ok(importSummary?.includes("Canonical commit: yes"), "terminal import summary should state canonical commit status");
    assert.ok(importSummary?.includes("2 conversations"), "terminal import summary should show committed conversations");
    assert.ok(importSummary?.includes("7 nodes"), "terminal import summary should show committed nodes");
    assert.ok(importSummary?.includes("Skipped conversations/elements: 1"), "terminal import summary should make skipped elements visible");
    assert.ok(importSummary?.includes("Warnings: 1"), "terminal import summary should show warning totals");
    assert.ok(importSummary?.includes("Conversation exceeded the import node limit: 1"), "warning codes should use a safe localized label");
    assert.equal(importSummary?.includes("conversation_node_limit_exceeded"), false, "terminal UI must not expose internal warning enum names");
    const terminalWarnings = await noDbPage.getByTestId("import-cleanup-warnings").textContent();
    assert.ok(terminalWarnings?.includes("committed import summary"), "terminal cleanup should show the localized summary warning");
    assert.ok(terminalWarnings?.includes("database connection"), "terminal cleanup should show every localized path kind");
    assert.equal(terminalWarnings?.includes("summary_update_after_commit_failed"), false, "terminal cleanup must not expose internal warning enum names");
    await noDbPage.waitForFunction(() => !document.querySelector('[data-testid="import-panel"]')?.textContent?.includes("Selected ZIP"), undefined, { timeout: 20_000 });
    assert.equal(await noDbPage.getByTestId("import-start-button").count(), 0, "successful job creation should clear selected file and start button");
    await noDbPage.waitForFunction(() => document.querySelectorAll(".conversation-item").length >= 1, undefined, { timeout: 20_000 });
    await noDbPage.unroute("**/api/import/upload");
    await noDbPage.unroute("**/api/import/jobs/**");
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
    run([
      ...pythonCommand(),
      "-c",
      "import sqlite3, sys; from chatgpt_export_archiver.db import migrate_database; conn=sqlite3.connect(sys.argv[1]); conn.execute(\"UPDATE conversation_nodes SET is_on_current_path = 0 WHERE conversation_id = 'dom-damaged-current'\"); conn.execute(\"UPDATE conversation_nodes SET is_on_current_path = CASE WHEN node_id = 'branch' THEN 1 ELSE 0 END WHERE conversation_id = 'dom-branch-override'\"); conn.execute(\"UPDATE conversation_nodes SET content_text = replace(hex(zeroblob(1100000)), '00', 'L') || ' DOM-LONG-BODY-END', raw_message_json = NULL WHERE conversation_id = 'dom-long-body' AND node_id = 'long-body'\"); conn.commit(); migrate_database(conn, refresh_compatibility=True); conn.commit(); conn.close()",
      db,
    ]);
    run([...pythonCommand(), "chatgpt_archive.py", "web-index", "--db", db]);

    const port = 19_000 + Math.floor(Math.random() * 2000);
    const baseUrl = `http://127.0.0.1:${port}/`;
    server = spawn(python.command, [...python.args, "chatgpt_archive.py", "web", "--db", db, "--host", "127.0.0.1", "--port", String(port)], {
      cwd: repoRoot,
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, CHATGPT_ARCHIVE_READER_MESSAGE_TEXT_CHARS: "8192" },
    });
    server.stdout.on("data", () => undefined);
    server.stderr.on("data", () => undefined);
    await waitForHealth(baseUrl);

    const fallbackPort = port + 1;
    const fallbackUrl = `http://127.0.0.1:${fallbackPort}/`;
    fallbackServer = spawn(python.command, [...python.args, "-c", "import pathlib,sys,uvicorn; from chatgpt_export_archiver.web_app import create_app; uvicorn.run(create_app(pathlib.Path(sys.argv[1]), static_dir=pathlib.Path(sys.argv[2]), allow_fallback=True), host='127.0.0.1', port=int(sys.argv[3]))", db, path.join(tmp, "missing-dist"), String(fallbackPort)], {
      cwd: repoRoot,
      stdio: ["ignore", "pipe", "pipe"],
    });
    fallbackServer.stdout.on("data", () => undefined);
    fallbackServer.stderr.on("data", () => undefined);
    await waitForHealth(fallbackUrl);

    const fallbackContext = await browser.newContext({ viewport: { width: 1000, height: 760 }, locale: "en-US" });
    const fallbackPage = await fallbackContext.newPage();
    await fallbackPage.addInitScript(() => {
      window.__fallbackCopiedText = "fallback-copy-sentinel";
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: async (text) => { window.__fallbackCopiedText = text; } },
      });
    });
    const fallbackRequests = [];
    fallbackPage.on("request", (request) => fallbackRequests.push(request.url()));
    await fallbackPage.goto(fallbackUrl, { waitUntil: "networkidle" });
    const fallbackItem = fallbackPage.locator("button.item").filter({ hasText: "<img src=x onerror=window.__fallbackInjected=true>" });
    await fallbackItem.waitFor({ state: "visible", timeout: 20_000 });
    await fallbackItem.click();
    await fallbackPage.getByRole("heading", { name: "<img src=x onerror=window.__fallbackInjected=true>" }).waitFor({ state: "visible", timeout: 20_000 });
    assert.equal(await fallbackPage.evaluate(() => Boolean(window.__fallbackInjected)), false, "fallback title/body must render as text, never executable HTML");
    assert.ok(fallbackRequests.some((url) => new URL(url).pathname === "/api/by-id/conversation"), "fallback detail must use by-id query routing");
    assert.ok(fallbackRequests.some((url) => new URL(url).pathname === "/api/by-id/messages"), "fallback messages must use by-id query routing");
    for (const label of ["Download visible MD", "Download visible TXT", "Bounded raw preview", "Display chunk"]) {
      const href = await fallbackPage.getByRole("link", { name: label }).first().getAttribute("href");
      assert.equal(new URL(href, fallbackUrl).searchParams.get("conversation_id"), "fallback/id?hash%:漢字", `${label} must preserve the complete fallback conversation ID`);
    }
    await fallbackPage.evaluate(() => {
      window.__fallbackNativeFetch = window.fetch;
      window.__fallbackCopyMode = "missing-length";
      window.__fallbackStreamCancelled = false;
      window.fetch = (input, init) => {
        const url = new URL(typeof input === "string" ? input : input.url, location.href);
        if (url.pathname !== "/api/by-id/copy") return window.__fallbackNativeFetch(input, init);
        if (window.__fallbackCopyMode === "missing-length") return Promise.resolve(new Response("small synthetic copy", { status: 200 }));
        if (window.__fallbackCopyMode === "oversized-length") return Promise.resolve(new Response("", { status: 200, headers: { "content-length": String(16 * 1024 * 1024 + 1) } }));
        if (window.__fallbackCopyMode === "underdeclared-body") return Promise.resolve(new Response("x".repeat(8 * 1024 * 1024 + 1), { status: 200, headers: { "content-length": "1" } }));
        if (window.__fallbackCopyMode === "stream-overflow") {
          let chunks = 0;
          return Promise.resolve(new Response(new ReadableStream({
            pull(controller) {
              chunks += 1;
              controller.enqueue(new Uint8Array(1024 * 1024).fill(120));
              if (chunks >= 32) controller.close();
            },
            cancel() { window.__fallbackStreamCancelled = true; },
          }), { status: 200 }));
        }
        return Promise.resolve(new Response("small synthetic copy", { status: 200, headers: { "content-length": "20" } }));
      };
    });
    const fallbackCopy = fallbackPage.getByRole("button", { name: "Copy visible current conversation" });
    await fallbackCopy.click();
    await fallbackPage.waitForFunction(() => window.__fallbackCopiedText === "small synthetic copy", undefined, { timeout: 20_000 });
    for (const mode of ["oversized-length", "underdeclared-body", "stream-overflow"]) {
      await fallbackPage.evaluate((nextMode) => {
        window.__fallbackCopyMode = nextMode;
        window.__fallbackCopiedText = "fallback-copy-sentinel";
        window.__fallbackStreamCancelled = false;
      }, mode);
      await fallbackCopy.click();
      await fallbackPage.getByText("Use Download instead", { exact: false }).waitFor({ state: "visible", timeout: 20_000 });
      assert.equal(await fallbackPage.evaluate(() => window.__fallbackCopiedText), "fallback-copy-sentinel", `${mode} must not mutate the clipboard`);
      assert.equal(await fallbackPage.getByRole("link", { name: "Download visible MD" }).count(), 1, `${mode} must leave Download available`);
      if (mode === "stream-overflow") assert.equal(await fallbackPage.evaluate(() => window.__fallbackStreamCancelled), true, "stream overflow must cancel the response reader before the unbounded tail is read");
    }
    await fallbackPage.evaluate(() => {
      window.__fallbackCopyMode = "success";
      window.__fallbackCopiedText = "";
    });
    await fallbackCopy.click();
    await fallbackPage.waitForFunction(() => window.__fallbackCopiedText === "small synthetic copy", undefined, { timeout: 20_000 });
    await fallbackPage.evaluate(() => { window.fetch = window.__fallbackNativeFetch; });
    await fallbackPage.getByRole("button", { name: "Open around message" }).click();
    await fallbackPage.waitForTimeout(300);
    assert.ok(fallbackRequests.some((url) => new URL(url).pathname === "/api/by-id/messages" && new URL(url).searchParams.has("around_node_id")), "fallback around navigation must use the by-id query route");
    await fallbackContext.close();
    fallbackServer.kill("SIGTERM");
    await new Promise((resolve) => fallbackServer.once("exit", resolve));
    fallbackServer = undefined;

    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "en-US" });
    const lifecyclePage = await context.newPage();
    await lifecyclePage.addInitScript(() => {
      if (!window.name) window.name = "0";
      const NativeAbortController = window.AbortController;
      window.AbortController = class extends NativeAbortController {
        abort(reason) {
          window.name = String(Number(window.name || "0") + 1);
          return super.abort(reason);
        }
      };
    });
    await lifecyclePage.route("**/api/stats", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 10_000));
      await route.continue();
    });
    await lifecyclePage.goto(baseUrl, { waitUntil: "domcontentloaded" });
    // This lifecycle probe needs DOM mount, not layout stability.  Chromium
    // can keep waitForSelector(state="visible") polling across rapid React
    // commits even after its trace reports a visible match, especially while
    // the deliberately delayed stats request is still pending.
    await lifecyclePage.waitForFunction(
      () => document.querySelector(".app-shell") !== null,
      undefined,
      { timeout: 20_000 },
    );
    const abortsBeforeUnmount = Number(await lifecyclePage.evaluate(() => window.name));
    await lifecyclePage.evaluate(() => window.dispatchEvent(new Event("chatgpt-archive:teardown")));
    await lifecyclePage.waitForFunction(() => !document.querySelector(".app-shell"), undefined, { timeout: 20_000 });
    const abortsAfterUnmount = Number(await lifecyclePage.evaluate(() => window.name));
    assert.ok(abortsAfterUnmount > abortsBeforeUnmount, "App unmount must abort pending refresh/list requests");
    await lifecyclePage.close();

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
    page.on("pageerror", (error) => browserDiagnostics.push(`pageerror: ${error.stack || error.message}`));
    page.on("response", (response) => {
      if (response.status() >= 400) browserDiagnostics.push(`http_${response.status()}: ${response.url()}`);
    });
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    try {
      await waitForCount(page, ".conversation-item", 20);
    } catch (error) {
      const health = await (await fetch(new URL("/api/health", baseUrl))).json();
      const apiPage = await (await fetch(new URL("/api/conversations?limit=5&sort=newest", baseUrl))).json();
      throw new Error(`initial conversation items did not render; health=${JSON.stringify(health)} api_count=${apiPage.items?.length ?? 0} diagnostics=${browserDiagnostics.join(" | ")}`);
    }
    let listStaleResponses = 0;
    const recoverListStale = async (route) => {
      const url = new URL(route.request().url());
      if ((Number(url.searchParams.get("offset")) || 0) > 0 && listStaleResponses === 0) {
        listStaleResponses += 1;
        await route.fulfill({ status: 409, contentType: "application/json", body: '{"detail":"search_continuation_stale","code":"search_continuation_stale"}' });
        return;
      }
      await route.continue();
    };
    await page.route("**/api/conversations?**", recoverListStale);
    const listStaleObserved = page.waitForResponse((response) => response.status() === 409 && new URL(response.url()).pathname === "/api/conversations");
    const listRefreshObserved = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.status() === 200 && url.pathname === "/api/conversations" && !url.searchParams.get("continuation");
    });
    await page.getByRole("button", { name: "Load more", exact: true }).click();
    await listStaleObserved;
    await listRefreshObserved;
    await page.waitForFunction(() => document.querySelectorAll(".conversation-item").length >= 20 && !document.querySelector(".sidebar .error-box"), undefined, { timeout: 20_000 });
    assert.equal(listStaleResponses, 1, "stale conversation append should restart page zero once");
    await page.unroute("**/api/conversations?**", recoverListStale);

    let repeatedListStaleResponses = 0;
    const rejectListTwice = async (route) => {
      const url = new URL(route.request().url());
      const isAppend = Boolean(url.searchParams.get("continuation")) || (Number(url.searchParams.get("offset")) || 0) > 0;
      if ((repeatedListStaleResponses === 0 && isAppend) || repeatedListStaleResponses === 1) {
        repeatedListStaleResponses += 1;
        await route.fulfill({ status: 409, contentType: "application/json", body: '{"detail":"search_continuation_stale","code":"search_continuation_stale"}' });
        return;
      }
      await route.continue();
    };
    await page.route("**/api/conversations?**", rejectListTwice);
    await page.getByRole("button", { name: "Load more", exact: true }).click();
    await page.getByText("The archive changed while loading. Try again.", { exact: true }).waitFor({ state: "visible", timeout: 20_000 });
    assert.equal(repeatedListStaleResponses, 2, "a second stale conversation response must stop retrying");
    assert.equal(await page.locator(".conversation-item").count(), 0, "repeated stale responses must not retain the old list snapshot");
    await page.unroute("**/api/conversations?**", rejectListTwice);
    await page.reload({ waitUntil: "networkidle" });
    await waitForCount(page, ".conversation-item", 20);
    const importButton = page.getByTestId("import-zip-button");
    await importButton.focus();
    assert.equal(await importButton.evaluate((node) => node === document.activeElement), true, "visible Import ZIP button must receive keyboard focus");
    assert.notEqual(await importButton.evaluate((node) => getComputedStyle(node).outlineStyle), "none", "Import ZIP button needs a visible focus ring");
    await page.locator("#import-zip-input").evaluate((node) => {
      node.click = () => { node.dataset.keyboardActivated = "true"; };
    });
    await page.keyboard.press("Enter");
    assert.equal(await page.locator("#import-zip-input").getAttribute("data-keyboard-activated"), "true", "Enter on the visible Import ZIP button must activate the file chooser path");
    await page.locator("#import-zip-input").evaluate((node) => { delete node.dataset.keyboardActivated; });
    await page.keyboard.press("Space");
    assert.equal(await page.locator("#import-zip-input").getAttribute("data-keyboard-activated"), "true", "Space on the visible Import ZIP button must activate the file chooser path");
    assert.equal(await importButton.getAttribute("aria-label"), null, "visible Import ZIP text supplies the accessible name without a hidden override");
    const selectedBeforeInteractiveKeys = new URL(page.url()).searchParams.get("conversation");
    await page.evaluate(() => {
      const fixture = document.createElement("div");
      fixture.id = "interactive-key-fixture";
      fixture.innerHTML = `
        <button data-kind="button"><span>Nested button</span></button>
        <a data-kind="link" href="#synthetic-link"><span>Nested link</span></a>
        <details><summary data-kind="summary"><span>Nested summary</span></summary><span>Details</span></details>
        <div data-kind="role-button" role="button" tabindex="0"><span>Nested role button</span></div>
        <div data-kind="tabindex" tabindex="0"><span>Nested tabindex</span></div>`;
      document.body.appendChild(fixture);
    });
    for (const kind of ["button", "link", "summary", "role-button", "tabindex"]) {
      for (const key of ["Enter", "n", "p", "j", "k", "ArrowDown", "ArrowUp"]) {
        await page.locator(`#interactive-key-fixture [data-kind="${kind}"] span`).evaluate((node, pressedKey) => {
          node.dispatchEvent(new KeyboardEvent("keydown", { key: pressedKey, bubbles: true, cancelable: true }));
        }, key);
        assert.equal(new URL(page.url()).searchParams.get("conversation"), selectedBeforeInteractiveKeys, `${key} from nested ${kind} content must not change conversation`);
      }
    }
    await page.locator("#interactive-key-fixture [data-kind=button]").focus();
    for (const key of ["Meta+k", "Control+k", "Alt+j"]) {
      await page.keyboard.press(key);
      assert.equal(new URL(page.url()).searchParams.get("conversation"), selectedBeforeInteractiveKeys, `${key} must not navigate conversations`);
    }
    await page.locator("#interactive-key-fixture").evaluate((node) => node.remove());
    const helpOpener = page.getByRole("button", { name: "Search help" });
    await helpOpener.focus();
    await helpOpener.click();
    await page.getByRole("dialog", { name: "Search help" }).waitFor({ state: "visible", timeout: 20_000 });
    assert.ok((await page.getByRole("dialog", { name: "Search help" }).textContent())?.includes("UTC calendar days"), "search help should state date filters use UTC calendar days");
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Close", "help dialog should focus its first control");
    await page.keyboard.press("Escape");
    await page.getByRole("dialog", { name: "Search help" }).waitFor({ state: "hidden" });
    assert.equal(await helpOpener.evaluate((node) => node === document.activeElement), true, "closing help should restore opener focus");
    await page.evaluate(() => {
      window.__nativeRaf = window.requestAnimationFrame;
      window.__nativeCancelRaf = window.cancelAnimationFrame;
      window.__heldRafs = new Map();
      window.__nextHeldRaf = 1;
      window.__cancelledHeldRafs = 0;
      window.requestAnimationFrame = (callback) => {
        const id = window.__nextHeldRaf++;
        window.__heldRafs.set(id, callback);
        return id;
      };
      window.cancelAnimationFrame = (id) => {
        if (window.__heldRafs.delete(id)) window.__cancelledHeldRafs += 1;
      };
    });
    await helpOpener.click();
    await page.getByRole("dialog", { name: "Search help" }).waitFor({ state: "visible" });
    await page.keyboard.press("Escape");
    await page.getByRole("dialog", { name: "Search help" }).waitFor({ state: "hidden" });
    await helpOpener.click();
    const reopenedHelp = page.getByRole("dialog", { name: "Search help" });
    await reopenedHelp.waitFor({ state: "visible" });
    await page.evaluate(() => {
      const callbacks = [...window.__heldRafs.values()];
      window.__heldRafs.clear();
      callbacks.forEach((callback) => callback(performance.now()));
    });
    assert.ok(await page.evaluate(() => window.__cancelledHeldRafs > 0), "modal reopen must cancel the stale focus-restore rAF");
    assert.equal(await reopenedHelp.evaluate((dialog) => dialog.contains(document.activeElement)), true, "stale close rAF must not move focus outside a reopened modal");
    await page.keyboard.press("Escape");
    await reopenedHelp.waitFor({ state: "hidden" });
    await page.evaluate(() => {
      window.requestAnimationFrame = window.__nativeRaf;
      window.cancelAnimationFrame = window.__nativeCancelRaf;
    });

    await page.evaluate(() => {
      window.__nativeFetch = window.fetch;
      window.__listRace = {};
      window.fetch = (input, init) => {
        const url = new URL(typeof input === "string" ? input : input.url, window.location.href);
        const query = url.searchParams.get("q");
        if (url.pathname === "/api/conversations" && (query === "race-a" || query === "race-b")) {
          return new Promise((resolve) => {
            window.__listRace[query] = () => resolve(new Response(JSON.stringify({
              items: [{ conversation_id: query, title: query === "race-a" ? "Stale Race A" : "Fresh Race B", create_time: 1, update_time: 1, current_node: null }],
              total: 1, limit: 60, offset: 0, has_more: false, next_offset: null, selected_in_results: false,
            }), { status: 200, headers: { "content-type": "application/json" } }));
          });
        }
        return window.__nativeFetch(input, init);
      };
    });
    await page.locator("#global-search").fill("race-a");
    await page.waitForFunction(() => Boolean(window.__listRace?.["race-a"]), undefined, { timeout: 20_000 });
    await page.locator("#global-search").fill("race-b");
    await page.waitForFunction(() => Boolean(window.__listRace?.["race-b"]), undefined, { timeout: 20_000 });
    await page.evaluate(() => window.__listRace["race-b"]());
    await page.getByRole("heading", { name: "Fresh Race B" }).waitFor({ state: "visible", timeout: 20_000 });
    await page.evaluate(() => window.__listRace["race-a"]());
    await page.waitForTimeout(200);
    assert.equal(await page.getByRole("heading", { name: "Fresh Race B" }).count(), 1, "late list response must not replace the newer selection");
    assert.equal(await page.getByRole("heading", { name: "Stale Race A" }).count(), 0, "stale selected metadata must be discarded");
    await page.evaluate(() => { window.fetch = window.__nativeFetch; });
    await page.locator("#global-search").fill("");
    await waitForCount(page, ".conversation-item", 20);

    const partialDiagnosticsRoute = async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname !== "/api/conversations" || url.searchParams.get("q") !== "partial-dom") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [{
            conversation_id: "dom-long",
            title: "DOM Long Conversation",
            create_time: 1,
            update_time: 1,
            current_node: "m379",
            hit_count: 10001,
            message_match: true,
            enrichment_partial: true,
          }],
          total: 1,
          limit: 60,
          offset: 0,
          has_more: false,
          next_offset: null,
          selected_in_results: true,
          diagnostics: { partial: true, completion_state: "partial" },
        }),
      });
    };
    await page.route("**/api/conversations**", partialDiagnosticsRoute);
    await page.locator("#global-search").fill("partial-dom");
    await page.getByText("Search is incomplete", { exact: false }).waitFor({ state: "visible", timeout: 20_000 });
    await page.getByText("Hit details limited", { exact: true }).waitFor({ state: "visible", timeout: 20_000 });
    await page.unroute("**/api/conversations**", partialDiagnosticsRoute);
    await page.locator("#global-search").fill("");
    await waitForCount(page, ".conversation-item", 20);

    const settingsOpener = page.getByRole("button", { name: "Settings" });
    await settingsOpener.focus();
    await settingsOpener.click();
    const settingsDialog = page.getByRole("dialog", { name: "Settings" });
    await settingsDialog.waitFor({ state: "visible" });
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Close", "settings dialog should focus its first control");
    await page.keyboard.press("Shift+Tab");
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Reset settings", "Shift+Tab should wrap to the final modal control");
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Close", "Tab should remain trapped inside settings");
    await page.evaluate(() => {
      window.__nativeStorageSetItem = Storage.prototype.setItem;
      Storage.prototype.setItem = function () { throw new DOMException("synthetic quota", "QuotaExceededError"); };
    });
    await page.getByLabel("Density").selectOption("compact");
    await page.waitForFunction(() => document.documentElement.dataset.density === "compact");
    await page.getByText("Settings are applied for this session", { exact: false }).waitFor({ state: "visible" });
    await page.keyboard.press("Escape");
    await settingsDialog.waitFor({ state: "hidden" });
    await page.waitForFunction(() => document.activeElement?.textContent === "Settings");
    assert.equal(await settingsOpener.evaluate((node) => node === document.activeElement), true, "closing settings should restore opener focus");
    await page.evaluate(() => { Storage.prototype.setItem = window.__nativeStorageSetItem; });
    const resizer = page.getByRole("separator", { name: "Sidebar width" });
    const widthBefore = Number(await resizer.getAttribute("aria-valuenow"));
    await resizer.focus();
    await page.keyboard.press("ArrowRight");
    assert.equal(Number(await resizer.getAttribute("aria-valuenow")), widthBefore + 10, "sidebar resizer should support keyboard arrows");
    const pointerWidths = await resizer.evaluate((node) => {
      node.setPointerCapture = () => undefined;
      node.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerId: 7, clientX: 410 }));
      window.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, pointerId: 7, clientX: 430 }));
      const during = Number(node.getAttribute("aria-valuenow"));
      window.dispatchEvent(new PointerEvent("pointercancel", { bubbles: true, pointerId: 7, clientX: 430 }));
      window.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, pointerId: 7, clientX: 520 }));
      return new Promise((resolve) => requestAnimationFrame(() => resolve({
        during,
        afterCancel: Number(node.getAttribute("aria-valuenow")),
      })));
    });
    assert.equal(pointerWidths.afterCancel, pointerWidths.during, "pointercancel must remove sidebar drag listeners immediately");

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
    assert.equal(await page.getByTestId("search-diagnostics-hint").count(), 0, "diagnostics hint should not show without a search context");

    const internalDiagnosticEnums = [
      "candidate_backend",
      "web_index_missing",
      "fts_legacy",
      "normalized_trigram",
      "normalized_title_trigram",
      "normalized_scan",
      "normalized_title_scan",
      "full_scan",
      "actual_fallback_note",
      "estimated_backend_note",
      "diagnostics_accuracy",
    ];
    await page.route("**/api/conversations**", async (route) => {
      const url = new URL(route.request().url());
      const diagQuery = url.searchParams.get("q");
      if (url.pathname === "/api/conversations" && ["diag-target", "diag-ja", "diag-es"].includes(diagQuery || "")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            items: [],
            total: 0,
            limit: 50,
            offset: 0,
            has_more: false,
            next_offset: null,
            selected_in_results: null,
            diagnostics: {
              candidate_backend: diagQuery === "diag-ja" ? "unknown_future_backend" : "full_scan",
              web_index_missing: diagQuery === "diag-target",
              short_query: diagQuery === "diag-es",
              legacy_fts_present: true,
              actual_fallback_note: diagQuery === "diag-ja" ? undefined : "legacy_fts_present_not_normalized_safe_candidate",
              estimated_backend_note: diagQuery === "diag-ja" ? undefined : "synthetic_estimate",
              diagnostics_accuracy: diagQuery === "diag-ja" ? "future_accuracy" : "best_effort",
            },
          }),
        });
        return;
      }
      await route.continue();
    });
    await page.locator("#global-search").fill("diag-target");
    await page.getByTestId("search-diagnostics-hint").waitFor({ state: "visible", timeout: 20_000 });
    const diagnosticsHint = await page.getByTestId("search-diagnostics-hint").textContent();
    assert.ok(diagnosticsHint?.includes("Web search index") || diagnosticsHint?.includes("search index"), "diagnostics hint should be user-readable localized text");
    for (const token of internalDiagnosticEnums) {
      assert.equal(diagnosticsHint?.includes(token), false, `diagnostics hint should not leak internal enum ${token}`);
    }
    for (const [lang, query, expectedFragment] of [
      ["ja", "diag-ja", "Web 検索インデックス"],
      ["es", "diag-es", "ruta segura"],
    ]) {
      await page.evaluate((language) => {
        const key = "chatgptArchiveWeb.settings.v2";
        const current = JSON.parse(localStorage.getItem(key) || "{}");
        localStorage.setItem(key, JSON.stringify({ ...current, language }));
      }, lang);
      await page.reload({ waitUntil: "networkidle" });
      await page.locator("#global-search").fill(query);
      await page.waitForFunction(
        (expected) => new URL(window.location.href).searchParams.get("q") === expected,
        query,
        { timeout: 20_000 },
      );
      const hintHandle = await page.waitForFunction(
        () => document.querySelector('[data-testid="search-diagnostics-hint"]')?.textContent || false,
        undefined,
        { timeout: 20_000 },
      );
      const localizedHint = await hintHandle.jsonValue();
      assert.ok(localizedHint?.includes(expectedFragment), `${lang} diagnostics hint should use localized readable text; got ${localizedHint}`);
      for (const token of internalDiagnosticEnums) {
        assert.equal(localizedHint?.includes(token), false, `${lang} diagnostics hint should not leak internal enum ${token}`);
      }
    }
    await page.evaluate(() => {
      const key = "chatgptArchiveWeb.settings.v2";
      const current = JSON.parse(localStorage.getItem(key) || "{}");
      localStorage.setItem(key, JSON.stringify({ ...current, language: "en" }));
    });
    await page.reload({ waitUntil: "networkidle" });
    await page.unroute("**/api/conversations**");
    await page.locator("#global-search").fill("");
    await page.getByTestId("search-diagnostics-hint").waitFor({ state: "hidden", timeout: 20_000 });
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
    const directAnchorRequests = [];
    await page.route("**/api/by-id/display**", async (route) => {
      const url = new URL(route.request().url());
      if (url.searchParams.get("cursor")) {
        directAnchorRequests.push({
          cursor: url.searchParams.get("cursor"),
          offset: url.searchParams.get("offset"),
          anchor: url.searchParams.get("anchor_char_offset"),
        });
      }
      await route.continue();
    });
    await page.getByLabel("Whole word").uncheck();
    await page.locator("#global-search").fill("ASTRAL-LATE-NEEDLE");
    await page.waitForFunction(() => document.querySelector(".results-meta")?.textContent?.includes("1 of 1 conversations"), undefined, { timeout: 20_000 });
    await page.getByRole("button", { name: /DOM Multiscript Search Conversation/ }).click();
    await page.waitForFunction(
      () => document.querySelector(".search-highlight-active")?.textContent === "ASTRAL-LATE-NEEDLE",
      undefined,
      { timeout: 20_000 },
    );
    assert.ok(directAnchorRequests.length >= 1, "astral late hit should request a revision-bound direct byte cursor");
    assert.equal(directAnchorRequests.at(-1)?.offset, "9000", "direct cursor should retain the source code-point offset");
    assert.equal(directAnchorRequests.at(-1)?.anchor, null, "direct byte cursor must replace the legacy prefix-scanning character anchor");

    await page.locator("#global-search").fill("");
    await waitForCount(page, ".conversation-item", 20);
    await page.getByRole("button", { name: /DOM Intel Word Conversation/ }).click();
    await page.locator("#global-search").fill('"café fi"');
    await page.waitForFunction(() => document.querySelector(".results-meta")?.textContent?.includes("1 of 1 conversations"), undefined, { timeout: 20_000 });
    await page.getByRole("button", { name: /DOM Multiscript Search Conversation/ }).click();
    await page.waitForFunction(
      () => document.querySelector(".search-highlight-active")?.textContent === "café ﬁ",
      undefined,
      { timeout: 20_000 },
    );
    assert.ok(directAnchorRequests.length >= 2, "virtual remount should request a fresh direct cursor for combining/NFKC text");
    await page.unroute("**/api/by-id/display**");
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
    const selectedBeforeAppend = await page.locator(".reader-header h1").textContent();
    const selectedUrlBeforeAppend = new URL(page.url()).searchParams.get("conversation");
    await page.locator(".conversation-list").evaluate((node) => { node.scrollTop = node.scrollHeight; });
    try {
      await waitForCount(page, ".conversation-item", beforeItems + 1);
    } catch {
      await page.getByRole("button", { name: "Load more" }).click();
      await waitForCount(page, ".conversation-item", beforeItems + 1);
    }
    const afterItems = await page.locator(".conversation-item").count();
    assert.ok(afterItems > beforeItems, "Load more should append conversations");
    assert.equal(await page.locator(".reader-header h1").textContent(), selectedBeforeAppend, "append page must not change the selected conversation");
    assert.equal(new URL(page.url()).searchParams.get("conversation"), selectedUrlBeforeAppend, "append page must not change shareable selected state");

    await page.goto(baseUrl, { waitUntil: "networkidle" });
    await waitForCount(page, ".conversation-item", 20);
    await page.evaluate(() => {
      window.__nativeFetch = window.fetch;
      window.__readerRace = {};
      window.fetch = (input, init) => {
        const url = new URL(typeof input === "string" ? input : input.url, window.location.href);
        const isDelayedDetail = url.pathname === "/api/by-id/conversation" && url.searchParams.get("conversation_id") === "dom-long";
        const isDelayedMessages = url.pathname === "/api/by-id/messages" && url.searchParams.get("conversation_id") === "dom-long";
        if (isDelayedDetail || isDelayedMessages) {
          const key = isDelayedDetail ? "detail" : "messages";
          return new Promise((resolve, reject) => {
            window.__readerRace[key] = () => window.__nativeFetch(input, { ...init, signal: undefined }).then(resolve, reject);
          });
        }
        return window.__nativeFetch(input, init);
      };
    });
    await page.getByRole("button", { name: /DOM Long Conversation/ }).click();
    await page.waitForFunction(() => Boolean(window.__readerRace?.detail && window.__readerRace?.messages), undefined, { timeout: 20_000 });
    await page.getByRole("button", { name: /DOM Reader Filter Conversation/ }).click();
    await page.getByRole("heading", { name: "DOM Reader Filter Conversation titleblock" }).waitFor({ state: "visible", timeout: 20_000 });
    await waitForCount(page, ".message", 1);
    await page.evaluate(() => { window.__readerRace.detail(); window.__readerRace.messages(); });
    await page.waitForTimeout(300);
    assert.equal(await page.getByRole("heading", { name: "DOM Reader Filter Conversation titleblock" }).count(), 1, "late detail response must not overwrite the newer selected conversation");
    assert.equal((await page.locator(".reader").textContent())?.includes("Synthetic message 379"), false, "late reader response must not render under a newer conversation title");
    await page.evaluate(() => { window.fetch = window.__nativeFetch; });

    await page.goto(`${baseUrl}?conversation=dom-long`, { waitUntil: "networkidle" });
    try {
      await waitForCount(page, ".message", 1);
      await page.waitForFunction(() => document.querySelector(".message-page-meta")?.textContent?.includes("of 380 visible messages"), undefined, { timeout: 20_000 });
    } catch (error) {
      const apiMessages = await (await fetch(new URL("/api/by-id/messages?conversation_id=dom-long&limit=5", baseUrl))).json();
      const readerText = await page.locator(".reader").textContent({ timeout: 1000 }).catch(() => "");
      throw new Error(`long conversation messages did not render; api_count=${apiMessages.items?.length ?? 0} total=${apiMessages.total ?? "unknown"} reader=${JSON.stringify((readerText || "").slice(0, 160))} diagnostics=${browserDiagnostics.join(" | ")}`);
    }
    const assistantBubble = await page.locator(".message-row-assistant .message").first().boundingBox();
    const userBubble = await page.locator(".message-row-user .message").first().boundingBox();
    assert.ok(assistantBubble && userBubble && userBubble.x > assistantBubble.x, "chat layout should align user messages to the right of assistant messages");

    await page.goto(`${baseUrl}?conversation=dom-long-body`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    const displayRequestsBeforeExpand = await page.evaluate(() => performance.getEntriesByType("resource").filter((entry) => {
      const url = new URL(entry.name);
      return url.pathname === "/api/by-id/display" && url.searchParams.get("node_id") === "long-body";
    }).length);
    await page.getByRole("button", { name: "Load full message body" }).click();
    await page.getByRole("button", { name: "Load more message body" }).click();
    await page.waitForFunction(() => document.querySelector('[data-node-id="long-body"] .message-text')?.textContent?.includes("DOM-LONG-BODY-END"), undefined, { timeout: 20_000 });
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByLabel("Message layout").selectOption("classic");
    await page.getByRole("button", { name: "Close" }).click();
    await page.locator(".message-scroll").evaluate((node) => { node.scrollTop = 0; });
    await page.locator('[data-node-id="long-body"] .message-text').waitFor({ state: "visible", timeout: 20_000 });
    assert.ok((await page.locator('[data-node-id="long-body"] .message-text').textContent())?.includes("DOM-LONG-BODY-END"), "layout changes must preserve expanded long message text");
    const displayRequestsAfterLayout = await page.evaluate(() => performance.getEntriesByType("resource").filter((entry) => {
      const url = new URL(entry.name);
      return url.pathname === "/api/by-id/display" && url.searchParams.get("node_id") === "long-body";
    }).length);
    assert.equal(displayRequestsAfterLayout - displayRequestsBeforeExpand, 2, "long-body expansion should be chunked and layout must not refetch it");
    assert.equal(
      await page.getByText("The stored raw payload exceeded the safe recovery limit", { exact: false }).count(),
      0,
      "normal intermediate canonical chunks with an inexact running total must not be labelled incomplete",
    );
    await page.evaluate(() => { window.__copiedText = ""; });
    await page.locator('[data-node-id="long-body"]').getByRole("button", { name: "Copy", exact: true }).click();
    await page.waitForFunction(() => String(window.__copiedText || "").includes("DOM-LONG-BODY-END"), undefined, { timeout: 20_000 });

    const staleCopyRequests = [];
    let staleCopyFailureInjected = false;
    const staleCopyRoute = async (route) => {
      const url = new URL(route.request().url());
      if (url.searchParams.get("node_id") !== "long-body") {
        await route.continue();
        return;
      }
      staleCopyRequests.push({
        offset: url.searchParams.get("offset"),
        cursor: url.searchParams.get("cursor"),
      });
      if (url.searchParams.get("cursor") && !staleCopyFailureInjected) {
        staleCopyFailureInjected = true;
        await route.fulfill({
          status: 409,
          contentType: "application/json",
          body: '{"detail":"display_cursor_stale","code":"display_cursor_stale"}',
        });
        return;
      }
      await route.continue();
    };
    await page.route("**/api/by-id/display?**", staleCopyRoute);
    await page.evaluate(() => { window.__copiedText = "stale-copy-sentinel"; });
    await page.locator('[data-node-id="long-body"]').getByRole("button", { name: "Copy", exact: true }).click();
    await page.waitForFunction(() => String(window.__copiedText || "").includes("DOM-LONG-BODY-END"), undefined, { timeout: 20_000 });
    assert.ok(staleCopyFailureInjected, "single-message copy should exercise a stale continuation");
    assert.ok(
      staleCopyRequests.filter((request) => request.offset === "0" && request.cursor === null).length >= 2,
      "stale display continuation must discard partial text and restart from offset zero exactly once",
    );
    assert.equal(
      await page.evaluate(() => window.__copiedText.length),
      1_100_000 + 1 + "DOM-LONG-BODY-END".length,
      "the restarted copy must not retain a prefix from the stale revision",
    );
    await page.unroute("**/api/by-id/display?**", staleCopyRoute);

    let repeatedStaleFailures = 0;
    const repeatedStaleRoute = async (route) => {
      const url = new URL(route.request().url());
      if (url.searchParams.get("node_id") === "long-body" && url.searchParams.get("cursor")) {
        repeatedStaleFailures += 1;
        await route.fulfill({
          status: 409,
          contentType: "application/json",
          body: '{"detail":"invalid_display_cursor","code":"invalid_display_cursor"}',
        });
        return;
      }
      await route.continue();
    };
    await page.route("**/api/by-id/display?**", repeatedStaleRoute);
    await page.evaluate(() => { window.__copiedText = "repeated-stale-sentinel"; });
    await page.locator('[data-node-id="long-body"]').getByRole("button", { name: "Copy", exact: true }).click();
    await page.getByText("The complete message body could not be loaded.", { exact: true }).waitFor({ state: "visible", timeout: 20_000 });
    assert.equal(repeatedStaleFailures, 2, "copy should stop after one clean restart");
    assert.equal(await page.evaluate(() => window.__copiedText), "repeated-stale-sentinel", "a second cursor failure must not write partial clipboard text");
    await page.unroute("**/api/by-id/display?**", repeatedStaleRoute);

    const incompleteDisplayRoute = async (route) => {
      const url = new URL(route.request().url());
      if (url.searchParams.get("node_id") !== "long-body") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          conversation_id: "dom-long-body",
          node_id: "long-body",
          display_text: "bounded placeholder only",
          offset: 0,
          returned_chars: 24,
          total_chars: 24,
          total_chars_exact: false,
          has_more: false,
          next_offset: null,
          next_cursor: null,
          content_revision: "synthetic-incomplete",
          max_chunk_chars: 65536,
          resolver_input_truncated: true,
          source: "canonical_placeholder",
        }),
      });
    };
    await page.route("**/api/by-id/display?**", incompleteDisplayRoute);
    await page.goto(`${baseUrl}?conversation=dom-long-body`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    await page.evaluate(() => { window.__copiedText = "incomplete-copy-sentinel"; });
    await page.locator('[data-node-id="long-body"]').getByRole("button", { name: "Copy", exact: true }).click();
    await page.getByText("The stored raw payload exceeded the safe recovery limit", { exact: false }).waitFor({ state: "visible", timeout: 20_000 });
    assert.equal(await page.evaluate(() => window.__copiedText), "incomplete-copy-sentinel", "single-message copy must not write an incomplete recovered body to the clipboard");
    await page.unroute("**/api/by-id/display?**", incompleteDisplayRoute);

    await page.goto(`${baseUrl}?conversation=dom-long&layout=classic`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    assert.equal(await page.locator(".message-row-chat").count(), 0, "classic layout query parameter should restore row-by-row message blocks");
    assert.equal(new URL(page.url()).searchParams.get("layout"), "classic", "initial URL state sync must preserve the layout override");
    await page.goto(`${baseUrl}?conversation=dom-long&layout=classic&show_internal=true`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    assert.equal(await page.getByLabel("Show internal messages").isChecked(), true, "shareable show_internal state should survive reload");
    await page.evaluate(() => {
      window.__copiedText = "";
      const input = document.querySelector("#global-search");
      const copyButton = [...document.querySelectorAll("button")].find((button) => button.textContent === "Copy URL");
      if (!(input instanceof HTMLInputElement) || !(copyButton instanceof HTMLButtonElement)) {
        throw new Error("search input or Copy URL button missing");
      }
      const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      if (!valueSetter) throw new Error("native input value setter missing");
      valueSetter.call(input, "copy-url-live-query");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      copyButton.click();
    });
    await page.waitForFunction(() => typeof window.__copiedText === "string" && window.__copiedText.length > 0);
    const copiedUrl = await page.evaluate(() => window.__copiedText);
    assert.equal(new URL(copiedUrl).searchParams.get("q"), null, "Copy URL before debounce must preserve the previously applied query");
    assert.equal(new URL(copiedUrl).searchParams.get("conversation"), "dom-long", "copied selection must belong to the same applied search context");
    assert.equal(new URL(copiedUrl).searchParams.get("match_mode"), "contains", "Copy URL must serialize the sender's explicit default match mode");
    assert.equal(new URL(copiedUrl).searchParams.get("layout"), "classic");
    assert.equal(new URL(copiedUrl).searchParams.get("show_internal"), "true");
    const receiverContext = await browser.newContext({ viewport: { width: 1200, height: 800 }, locale: "en-US" });
    await receiverContext.addInitScript(() => {
      localStorage.setItem("chatgptArchiveWeb.searchMatchMode.v1", "word");
      localStorage.setItem("chatgptArchiveWeb.settings.v2", JSON.stringify({ messageLayout: "chat", showInternalDefault: false }));
    });
    const receiverPage = await receiverContext.newPage();
    await receiverPage.goto(copiedUrl, { waitUntil: "networkidle" });
    await waitForCount(receiverPage, ".message", 1);
    assert.equal(await receiverPage.getByLabel("Whole word").isChecked(), false, "copied contains mode must beat recipient localStorage word mode");
    assert.equal(await receiverPage.getByLabel("Show internal messages").isChecked(), true, "copied internal visibility must beat recipient localStorage");
    await receiverPage.getByRole("button", { name: "Settings" }).click();
    assert.equal(await receiverPage.getByLabel("Message layout").inputValue(), "classic", "copied layout must beat recipient localStorage");
    await receiverContext.close();
    await page.goto(`${baseUrl}?conversation=dom-long&match_mode=word&layout=chat&show_internal=false`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    await page.getByRole("button", { name: "Copy URL" }).click();
    const copiedOppositeUrl = await page.evaluate(() => window.__copiedText);
    assert.equal(new URL(copiedOppositeUrl).searchParams.get("match_mode"), "word");
    assert.equal(new URL(copiedOppositeUrl).searchParams.get("layout"), "chat");
    assert.equal(new URL(copiedOppositeUrl).searchParams.get("show_internal"), "false");
    const oppositeContext = await browser.newContext({ viewport: { width: 1200, height: 800 }, locale: "en-US" });
    await oppositeContext.addInitScript(() => {
      localStorage.setItem("chatgptArchiveWeb.searchMatchMode.v1", "contains");
      localStorage.setItem("chatgptArchiveWeb.settings.v2", JSON.stringify({ messageLayout: "classic", showInternalDefault: true }));
    });
    const oppositePage = await oppositeContext.newPage();
    await oppositePage.goto(copiedOppositeUrl, { waitUntil: "networkidle" });
    await waitForCount(oppositePage, ".message", 1);
    assert.equal(await oppositePage.getByLabel("Whole word").isChecked(), true);
    assert.equal(await oppositePage.getByLabel("Show internal messages").isChecked(), false);
    await oppositePage.getByRole("button", { name: "Settings" }).click();
    assert.equal(await oppositePage.getByLabel("Message layout").inputValue(), "chat");
    await oppositeContext.close();
    await page.goto(`${baseUrl}?conversation=dom-long`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    let visualDataRequests = 0;
    const visualRequestListener = (request) => {
      const url = new URL(request.url());
      if ((url.pathname === "/api/by-id/messages" && url.searchParams.get("conversation_id") === "dom-long") || url.pathname === "/api/search/messages") visualDataRequests += 1;
    };
    page.on("request", visualRequestListener);
    const firstMessageBeforeVisualChanges = await page.locator(".message").first().textContent();
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
    await page.waitForTimeout(200);
    page.off("request", visualRequestListener);
    assert.equal(visualDataRequests, 0, "pure layout/density/font/max-width changes must not request messages or hits again");
    assert.equal(await page.locator(".message").first().textContent(), firstMessageBeforeVisualChanges, "visual changes must preserve loaded reader bubbles");
    assert.equal(await page.locator(".reader .loading-progress").count(), 0, "visual changes must not flash the reader back to loading");
    const messageMetrics = await page.locator(".message-scroll").evaluate((node) => ({
      clientHeight: node.clientHeight,
      scrollHeight: node.scrollHeight,
      before: node.scrollTop,
    }));
    assert.ok(messageMetrics.scrollHeight > messageMetrics.clientHeight, "message list must scroll internally");
    await page.locator(".message-scroll").evaluate((node) => { node.scrollTop = 500; });
    await page.waitForFunction(() => document.querySelector(".message-scroll")?.scrollTop > 0);

    let copyReaderPageRequests = 0;
    const copyRequestListener = (request) => {
      const url = new URL(request.url());
      if (url.pathname === "/api/by-id/messages" && url.searchParams.get("conversation_id") === "dom-long") copyReaderPageRequests += 1;
    };
    page.on("request", copyRequestListener);
    await page.getByRole("button", { name: "Copy current path conversation" }).click();
    await page.waitForFunction(() => window.__copiedText?.includes("Synthetic message 379"), undefined, { timeout: 20_000 });
    page.off("request", copyRequestListener);
    assert.equal(copyReaderPageRequests, 0, "full conversation copy must not request reader pages");
    assert.equal(await page.evaluate(() => window.__copiedText.includes("Synthetic system context for DOM test")), false, "copy current path conversation should respect hidden internal messages");
    await page.evaluate(() => { window.__copiedText = ""; });
    await page.getByRole("button", { name: "Copy visible" }).click();
    await page.waitForFunction(() => window.__copiedText?.includes("Synthetic message 120"), undefined, { timeout: 20_000 });
    assert.equal(await page.evaluate(() => window.__copiedText.includes("Synthetic system context for DOM test")), false, "copy visible should copy loaded reader-visible messages, not hidden internal messages");
    const copiedBeforeFailure = await page.evaluate(() => window.__copiedText || "");
    await page.waitForTimeout(300);
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    });
    await page.getByRole("button", { name: "Copy visible" }).click();
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Copy failed"), undefined, { timeout: 20_000 });
    await page.waitForTimeout(1_200);
    assert.ok((await page.locator(".hit-counter").textContent())?.includes("Copy failed"), "the first copy timeout must not clear the newer copy status");
    assert.equal(await page.evaluate(() => window.__copiedText || ""), copiedBeforeFailure, "missing Clipboard API must not be reported as a successful copy");
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: async (text) => { window.__copiedText = text; } },
      });
    });
    await page.evaluate(() => {
      window.__copiedText = "copy-race-sentinel";
      window.__nativeFetch = window.fetch;
      window.__copyRaceRelease = null;
      window.fetch = (input, init) => {
        const url = new URL(typeof input === "string" ? input : input.url, window.location.href);
        if (url.pathname === "/api/by-id/copy" && url.searchParams.get("conversation_id") === "dom-long") {
          return new Promise((resolve, reject) => {
            window.__copyRaceRelease = () => window.__nativeFetch(input, { ...init, signal: undefined }).then(resolve, reject);
          });
        }
        return window.__nativeFetch(input, init);
      };
    });
    await page.getByRole("button", { name: "Copy current path conversation" }).click();
    await page.waitForFunction(() => Boolean(window.__copyRaceRelease), undefined, { timeout: 20_000 });
    await page.getByRole("button", { name: /DOM Role Class Conversation/ }).click();
    await page.getByRole("heading", { name: "DOM Role Class Conversation" }).waitFor({ state: "visible", timeout: 20_000 });
    await page.evaluate(() => window.__copyRaceRelease());
    await page.waitForTimeout(300);
    assert.equal(await page.evaluate(() => window.__copiedText), "copy-race-sentinel", "copy response from an old reader context must not write to the clipboard");
    assert.equal((await page.locator(".hit-counter").textContent())?.includes("Copied"), false, "old copy completion must not set success in the new reader context");
    await page.evaluate(() => { window.fetch = window.__nativeFetch; });

    await page.goto(`${baseUrl}?conversation=dom-long`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    await page.evaluate(() => {
      window.__copiedText = "clipboard-delay-sentinel";
      window.__clipboardRelease = null;
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: (text) => new Promise((resolve) => {
            window.__clipboardRelease = () => {
              window.__copiedText = text;
              resolve();
            };
          }),
        },
      });
    });
    await page.getByRole("button", { name: "Copy visible" }).click();
    await page.waitForFunction(() => Boolean(window.__clipboardRelease), undefined, { timeout: 20_000 });
    await page.getByRole("button", { name: /DOM Role Class Conversation/ }).click();
    await page.getByRole("heading", { name: "DOM Role Class Conversation" }).waitFor({ state: "visible", timeout: 20_000 });
    await page.evaluate(() => window.__clipboardRelease());
    await page.waitForTimeout(200);
    assert.equal((await page.locator(".hit-counter").textContent())?.includes("Copied"), false, "a delayed clipboard completion from the old context must never be reported as a valid copy");
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: async (text) => { window.__copiedText = text; } },
      });
    });

    await page.goto(`${baseUrl}?conversation=dom-long`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    await page.evaluate((value) => { window.__copiedText = value; }, copiedBeforeFailure);
    const failCopyStream = async (route) => {
      await route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"synthetic copy failure"}' });
    };
    await page.route("**/api/by-id/copy?**", failCopyStream);
    await page.getByRole("button", { name: "Copy current path conversation" }).click();
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Copy conversation failed"), undefined, { timeout: 20_000 });
    assert.equal(await page.evaluate(() => window.__copiedText || ""), copiedBeforeFailure, "failed full-copy stream must not overwrite clipboard with a partial conversation");
    await page.unroute("**/api/by-id/copy?**", failCopyStream);

    await page.getByRole("button", { name: "Load more messages" }).click();
    await page.waitForFunction(() => document.querySelector(".message-page-meta")?.textContent?.includes("of 380 visible messages") && !document.querySelector(".message-page-meta button"), undefined, { timeout: 20_000 });

    const showRawCount = await page.getByRole("button", { name: "Show raw preview" }).count();
    assert.ok(showRawCount > 0, "raw preview toggle should be available");
    await page.getByRole("button", { name: "Show raw preview" }).first().click();
    await page.locator(".raw-message").first().waitFor({ state: "visible", timeout: 10_000 });
    await assertStableMessageViewport(page, "raw preview expansion");
    const cappedRawText = 'plain raw_text preview with "quotes" and \\ backslash';
    await page.route("**/api/by-id/raw?*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          raw_message: { compat: "field should not render when raw_text is present" },
          raw_text: cappedRawText,
          raw_size: cappedRawText.length + 20,
          truncated: true,
        }),
      });
    }, { times: 1 });
    await page.getByRole("button", { name: "Open larger raw preview" }).first().click();
    await page.locator(".raw-full").first().waitFor({ state: "visible", timeout: 20_000 });
    assert.equal(await page.locator(".raw-full").first().textContent(), cappedRawText, "truncated larger raw preview should render raw_text as plain text");
    assert.equal((await page.locator(".raw-full").first().textContent())?.includes('\\"'), false, "truncated raw_text should not be JSON string escaped");
    assert.ok((await page.locator(".raw-error").first().textContent())?.includes("truncated"), "truncated capped raw preview should show a localized note");
    await assertStableMessageViewport(page, "async larger raw preview expansion");
    await page.route("**/api/by-id/raw?*", async (route) => {
      await route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"synthetic raw failure"}' });
    }, { times: 1 });
    await page.getByRole("button", { name: "Close larger raw preview" }).first().click();
    await page.waitForFunction(() => document.querySelectorAll(".raw-full").length === 0, undefined, { timeout: 20_000 });
    await assertStableMessageViewport(page, "larger raw preview collapse");
    await page.getByRole("button", { name: "Open larger raw preview" }).first().click();
    await page.locator(".raw-error").first().waitFor({ state: "visible", timeout: 20_000 });
    await assertStableMessageViewport(page, "larger raw preview error state");
    await page.unroute("**/api/by-id/raw?*");
    await page.getByRole("button", { name: "Open larger raw preview" }).first().click();
    await page.locator(".raw-full").first().waitFor({ state: "visible", timeout: 20_000 });
    assert.equal(await page.locator(".raw-error").count(), 0, "full raw retry should clear the visible error state");
    await assertStableMessageViewport(page, "larger raw preview retry success");
    await page.getByRole("button", { name: "Close larger raw preview" }).first().click();
    await page.waitForFunction(() => document.querySelectorAll(".raw-full").length === 0, undefined, { timeout: 20_000 });
    await page.getByRole("button", { name: "Hide raw preview" }).first().click();
    await page.waitForFunction(() => document.querySelectorAll(".raw-message").length === 0, undefined, { timeout: 20_000 });
    await assertStableMessageViewport(page, "raw preview collapse");

    await page.locator(".message-scroll").evaluate((node) => { node.scrollTop = 0; });
    await page.getByLabel("Show internal messages").check();
    await page.locator(".message-disclosure.message-internal").first().waitFor({ state: "visible", timeout: 20_000 });
    await page.getByRole("button", { name: "Copy current path conversation" }).click();
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
      await openInternal.getByRole("button", { name: "Open larger raw preview" }).click();
      await openInternal.locator(".raw-full").first().waitFor({ state: "visible", timeout: 20_000 });
      await assertStableMessageViewport(page, "internal larger raw preview expansion");
      await openInternal.getByRole("button", { name: "Close larger raw preview" }).click();
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

    await page.goto(`${baseUrl}?conversation=dom-reader-filter`, { waitUntil: "networkidle" });
    await page.locator("#global-search").fill("amber birch cedar denim ember frost glade hazel ivory jewel khaki");
    await page.getByText("This message has more matches than the highlight preview can mark", { exact: false }).waitFor({ state: "visible", timeout: 20_000 });

    await page.goto(`${baseUrl}?conversation=dom-role-class`, { waitUntil: "networkidle" });
    await page.getByLabel("Show internal messages").check();
    await page.waitForFunction(() => document.querySelector(".message-role-tool-system"), undefined, { timeout: 20_000 });
    const toolClassName = await page.locator(".message-role-tool-system").first().evaluate((node) => node.className);
    assert.ok(!toolClassName.includes("/"), "message role classes must be CSS-safe");

    await page.goto(`${baseUrl}?conversation=dom-tech-json`, { waitUntil: "networkidle" });
    assert.ok(await page.locator(".message-row-user .message-disclosure").count() === 0, "ordinary user JSON should not be folded as technical payload");
    assert.ok(await page.locator(".message-row-assistant .message-disclosure").count() === 0, "ordinary assistant code block should not be folded as technical payload");
    assert.ok(await page.locator(".message-row-assistant .message-text").filter({ hasText: "ordinary JSON result" }).count() >= 1, "ordinary assistant JSON with a query key should stay expanded");
    assert.ok(await page.locator(".message-row-assistant .message-text").filter({ hasText: "ordinary assistant JSON example" }).count() >= 1, "ordinary assistant tool-like JSON should stay expanded");
    await page.getByLabel("Show internal messages").check();
    await page.locator(".message-disclosure.message-internal").first().waitFor({ state: "visible", timeout: 20_000 });
    assert.ok(await page.locator(".message-disclosure.message-role-tool-system").count() >= 1, "internal tool JSON should fold as a technical payload");
    assert.ok(await page.locator(".message-disclosure.message-role-assistant").count() >= 1, "source analysis assistant payload should be hidden until internal messages are shown, then fold as technical");
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
	    await page.getByLabel("Message path").selectOption("current");
	    await page.evaluate(() => { window.__copiedText = ""; });
	    await page.getByRole("button", { name: "Copy current path conversation" }).click();
	    await page.waitForFunction(() => window.__copiedText?.includes("Current answer body."), undefined, { timeout: 20_000 });
	    assert.equal(await page.evaluate(() => window.__copiedText.includes("branchoverride-token")), false, "copy current path conversation should not include branch-only nodes while path=current");

	    await page.goto(`${baseUrl}?conversation=dom-damaged-current`, { waitUntil: "networkidle" });
	    await page.getByLabel("Message path").selectOption("current");
	    await page.locator("#global-search").fill("damaged-current-visible-needle");
	    await page.waitForFunction(() => document.querySelector(".reader-header h1")?.textContent?.includes("DOM Damaged Current Fallback"), undefined, { timeout: 20_000 });
    await waitForActiveHighlightVisible(page);
    const damagedItemText = await page.getByRole("button", { name: /DOM Damaged Current Fallback/ }).textContent();
    assert.ok(!/branch/i.test(damagedItemText || ""), "sidebar should not mark damaged current fallback hits as branch hits");
    assert.equal(await page.locator(".message .branch-pill").count(), 0, "fallback-visible messages should not be marked as branch messages");
    assert.ok(!((await page.locator(".hit-counter").textContent()) || "").includes("Hidden hits"), "fallback current hits should be navigable visible hits");
    await page.getByRole("button", { name: "Next hit" }).click();
    await waitForActiveHighlightVisible(page);

	    await page.goto(`${baseUrl}?conversation=dom-branch-override`, { waitUntil: "networkidle" });
	    await page.getByLabel("Message path").selectOption("current");
	    await page.getByLabel("Message path").selectOption("all");
	    await page.evaluate(() => { window.__copiedText = ""; });
    await page.getByRole("button", { name: "Copy all-nodes conversation" }).click();
    await page.waitForFunction(() => window.__copiedText?.includes("branchoverride-token"), undefined, { timeout: 20_000 });
    await page.getByLabel("Message path").selectOption("current");
    await page.locator("#global-search").fill("PATH:ALL branchoverride-token");
    await page.waitForFunction(() => document.querySelector(".reader-header h1")?.textContent?.includes("DOM Branch Override Conversation"), undefined, { timeout: 20_000 });
	    await page.waitForFunction(() => document.querySelector(".search-visibility-notes")?.textContent?.includes("Query overrides path"), undefined, { timeout: 20_000 });
	    await page.waitForFunction(() => document.querySelector(".message-scroll")?.textContent?.includes("branchoverride-token"), undefined, { timeout: 20_000 });
	    assert.ok(await page.locator(".message", { hasText: "branchoverride-token" }).locator(".branch-pill").count() >= 1, "effective off-current branch keeps its badge even when the raw flag is stray true");
    assert.equal(await page.locator(".message", { hasText: "branchoverride-token" }).getAttribute("data-raw-current-path"), "true", "debug provenance keeps the original raw current-path flag");
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
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("No hits"), undefined, { timeout: 20_000 });
    assert.equal((await page.locator(".hit-counter").textContent())?.includes("Filter match"), false, "exclude-only context is not a positive filter-only match");
    assert.equal(await page.locator(".search-highlight").count(), 0, "exclude-only filter should not create body highlights");
    await page.getByLabel("Exclude").fill("");
    await page.getByLabel("Role").selectOption("assistant");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("Filter match"), undefined, { timeout: 20_000 });
    assert.equal(await page.locator(".search-highlight").count(), 0, "role-only filter should not be presented as a body hit");
    await page.getByLabel("Role").selectOption("");
    await page.locator("#global-search").fill("-filtertarget");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("No hits"), undefined, { timeout: 20_000 });
    assert.equal((await page.locator(".hit-counter").textContent())?.includes("Filter match"), false, "raw exclude-only query is not a positive filter-only match");
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
	    await page.waitForFunction(() => !document.querySelector('[data-testid="search-loading-progress"]') && !Array.from(document.querySelectorAll(".conversation-title")).some((node) => node.textContent?.includes("DOM Reader Filter Conversation")), undefined, { timeout: 20_000 });
	    assert.equal(await page.getByRole("button", { name: /DOM Reader Filter Conversation/ }).count(), 0, "conversation-level exclude should remove conversations containing the excluded body fragment");
	    await page.getByLabel("Exclude").fill("");
	    await page.goto(`${baseUrl}?conversation=dom-reader-filter&q=filtertarget`, { waitUntil: "networkidle" });
	    await page.locator("details.advanced-panel").evaluate((node) => { node.open = true; });
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
    await page.getByLabel("Show internal messages").check();
    await page.getByLabel("Scope").selectOption("all");
    await page.locator("#global-search").fill("synthetic docs");
    await page.waitForFunction(() => document.querySelector(".message-disclosure.message-role-tool-system")?.open, undefined, { timeout: 20_000 });
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

    await page.goto(`${baseUrl}?conversation=dom-hit-sequence&match_mode=contains&path=current&scope=all`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    let hitStalePhase = 0;
    const recoverHitStale = async (route) => {
      const url = new URL(route.request().url());
      if (url.searchParams.get("conversation_id") !== "dom-hit-sequence") {
        await route.continue();
        return;
      }
      if (hitStalePhase === 0) {
        hitStalePhase = 1;
        const original = await route.fetch();
        const body = await original.json();
        body.items = body.items.slice(0, 1);
        body.total = 1;
        body.total_exact = false;
        body.has_more = true;
        await route.fulfill({ response: original, body: JSON.stringify(body), contentType: "application/json" });
        return;
      }
      if (url.searchParams.get("continuation") && hitStalePhase === 1) {
        hitStalePhase = 2;
        await route.fulfill({ status: 409, contentType: "application/json", body: '{"detail":"search_continuation_stale","code":"search_continuation_stale"}' });
        return;
      }
      await route.continue();
    };
    await page.route("**/api/search/messages?**", recoverHitStale);
    await page.locator("#global-search").fill("sequence-target");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("1 / 1"), undefined, { timeout: 20_000 });
    await page.getByRole("button", { name: "Next hit" }).click();
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("1 / 100"), undefined, { timeout: 20_000 });
    assert.equal(hitStalePhase, 2, "stale message-hit append should replace with a fresh first segment");
    await page.unroute("**/api/search/messages?**", recoverHitStale);

    await page.goto(`${baseUrl}?conversation=dom-hit-sequence&match_mode=contains&path=current&scope=all`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
    let repeatedHitPhase = 0;
    const rejectHitTwice = async (route) => {
      const url = new URL(route.request().url());
      if (url.searchParams.get("conversation_id") !== "dom-hit-sequence") {
        await route.continue();
        return;
      }
      if (repeatedHitPhase === 0) {
        repeatedHitPhase = 1;
        const original = await route.fetch();
        const body = await original.json();
        body.items = body.items.slice(0, 1);
        body.total = 1;
        body.total_exact = false;
        body.has_more = true;
        await route.fulfill({ response: original, body: JSON.stringify(body), contentType: "application/json" });
        return;
      }
      repeatedHitPhase += 1;
      await route.fulfill({ status: 409, contentType: "application/json", body: '{"detail":"search_continuation_stale","code":"search_continuation_stale"}' });
    };
    await page.route("**/api/search/messages?**", rejectHitTwice);
    await page.locator("#global-search").fill("sequence-target");
    await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("1 / 1"), undefined, { timeout: 20_000 });
    await page.getByRole("button", { name: "Next hit" }).click();
    await new Promise((resolve, reject) => {
      const deadline = Date.now() + 20_000;
      const poll = () => {
        if (repeatedHitPhase >= 3) resolve();
        else if (Date.now() >= deadline) reject(new Error(`message-hit repeated stale phase stopped at ${repeatedHitPhase}`));
        else setTimeout(poll, 20);
      };
      poll();
    });
    await page.getByText("The archive changed while loading. Try again.", { exact: true }).waitFor({ state: "visible", timeout: 20_000 });
    assert.equal(repeatedHitPhase, 3, "message-hit recovery must stop after one failed restart");
    assert.equal((await page.locator(".hit-counter").textContent())?.includes("1 /"), false, "old hit items must not survive repeated stale responses");
    await page.unroute("**/api/search/messages?**", rejectHitTwice);

    await page.goto(`${baseUrl}?conversation=dom-hit-sequence&match_mode=contains&path=current&scope=all`, { waitUntil: "networkidle" });
    await waitForCount(page, ".message", 1);
	    let initialHitNavigationRequests = 0;
	    const initialHitResponses = [];
	    const countInitialHitNavigation = (request) => {
      const url = new URL(request.url());
      if (url.pathname === "/api/search/messages" && url.searchParams.get("conversation_id") === "dom-hit-sequence") {
        initialHitNavigationRequests += 1;
      }
	    };
	    const captureInitialHitResponse = async (response) => {
	      const url = new URL(response.url());
	      if (url.pathname === "/api/search/messages" && url.searchParams.get("conversation_id") === "dom-hit-sequence") {
	        const body = await response.json().catch(() => ({}));
	        initialHitResponses.push({
	          status: response.status(),
	          itemCount: Array.isArray(body.items) ? body.items.length : null,
	          total: body.total ?? null,
	          detail: body.detail ?? null,
	        });
	      }
	    };
	    page.on("request", countInitialHitNavigation);
	    page.on("response", captureInitialHitResponse);
	    await page.locator("#global-search").fill("sequence-target");
	    try {
	      await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("1 / 100"), undefined, { timeout: 20_000 });
	    } catch (error) {
	      throw new Error(`initial sequence segment missing: counter=${await page.locator(".hit-counter").textContent()} ui_error=${await page.locator(".error-box").allTextContents()} responses=${JSON.stringify(initialHitResponses)} cause=${error instanceof Error ? error.message : String(error)}`);
	    }
	    await page.waitForTimeout(350);
	    page.off("request", countInitialHitNavigation);
	    page.off("response", captureInitialHitResponse);
    assert.equal(initialHitNavigationRequests, 1, "initial reader hit navigation should issue exactly one request");
    const expectedSequence = expectedSequenceHitIds();
    for (let idx = 0; idx <= 155; idx += 1) {
      try {
        await waitForActiveNodeWithVisibleHighlight(page, expectedSequence[idx]);
      } catch (error) {
        const diagnostic = await page.evaluate(() => ({
          activeNode: document.querySelector(".message-active")?.getAttribute("data-node-id") ?? null,
          activeMarks: document.querySelectorAll(".message-active .search-highlight").length,
          marks: document.querySelectorAll(".search-highlight").length,
          pageMeta: document.querySelector(".message-page-meta")?.textContent ?? "",
        }));
        throw new Error(`sequence navigation failed at index=${idx} expected=${expectedSequence[idx]} diagnostic=${JSON.stringify(diagnostic)} cause=${error instanceof Error ? error.message : String(error)}`);
      }
      if (idx === 91) {
        await page.waitForFunction(() => document.querySelector(".hit-counter")?.textContent?.includes("/ 180"), undefined, { timeout: 20_000 });
      }
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
    const mobileToolbar = await page.getByTestId("top-toolbar").boundingBox();
    assert.ok(mobileToolbar && mobileToolbar.height >= 40, "390px layout must keep a mobile toolbar visible");
    assert.ok(await page.getByTestId("import-zip-button").isVisible(), "mobile toolbar must keep ZIP import accessible");
    assert.ok(await page.getByRole("button", { name: "设置" }).isVisible(), "mobile toolbar must keep Settings accessible");
    assert.ok(await page.getByRole("button", { name: "搜索帮助" }).isVisible(), "mobile toolbar must keep Search Help accessible");
    await page.getByRole("button", { name: "设置" }).focus();
    const focusOutline = await page.getByRole("button", { name: "设置" }).evaluate((node) => getComputedStyle(node).outlineStyle);
    assert.notEqual(focusOutline, "none", "mobile controls need a visible keyboard focus indicator");
    await page.locator(".advanced-panel summary").click();
    const narrow = await page.locator(".message-scroll, .empty-state").first().boundingBox();
    assert.ok(narrow && narrow.height > 100, "narrow layout should keep reader usable even with advanced filters open");

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
    if (fallbackServer) {
      if (fallbackServer.exitCode === null && fallbackServer.signalCode === null) {
        fallbackServer.kill("SIGTERM");
        await new Promise((resolve) => fallbackServer.once("exit", resolve));
      }
    }
    await fsp.rm(tmp, { recursive: true, force: true });
  }
}

await main();
