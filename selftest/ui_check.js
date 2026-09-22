// Drives the dashboard in headless Chrome over the DevTools protocol and checks
// every control. Run through selftest/ui_check.sh, which starts a local admin
// server backed by the mock pw CLI. Needs Node 22+ (global WebSocket) and Chrome.
const { spawn } = require("child_process");
const fs = require("fs");

const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const URL = process.argv[2];
const CALLS_LOG = process.argv[3];
const PORT = 9333;
let failures = 0;

function check(name, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  " + detail : ""}`);
  if (!ok) failures += 1;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const profile = fs.mkdtempSync("/tmp/probe-ui-");
  const chrome = spawn(CHROME, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`, "--window-size=1400,1000", "about:blank"], { stdio: "ignore" });
  try {
    let targets = [];
    for (let i = 0; i < 50 && !targets.length; i++) {
      await sleep(200);
      try {
        targets = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).filter((t) => t.type === "page");
      } catch (e) { /* not ready */ }
    }
    if (!targets.length) throw new Error("Chrome did not expose a page target");
    const ws = new WebSocket(targets[0].webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
    let nextId = 1;
    const pending = new Map();
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id); }
    };
    const send = (method, params = {}) => new Promise((resolve) => {
      const id = nextId++;
      pending.set(id, resolve);
      ws.send(JSON.stringify({ id, method, params }));
    });
    const evaluate = async (expression) => {
      const reply = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
      if (reply.result && reply.result.exceptionDetails) throw new Error(JSON.stringify(reply.result.exceptionDetails.exception || reply.result.exceptionDetails));
      return reply.result.result.value;
    };
    const until = async (expression, timeoutMs = 15000, label = expression) => {
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        if (await evaluate(expression)) return true;
        await sleep(250);
      }
      throw new Error("timed out waiting for " + label);
    };
    const click = (selector) => evaluate(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e) return "missing"; e.click(); return "clicked"; })()`);
    const rows = () => evaluate(`document.querySelectorAll("#tests-body tr[data-id]").length`);
    const text = (selector) => evaluate(`(document.querySelector(${JSON.stringify(selector)}) || {}).textContent || ""`);
    const hidden = (selector) => evaluate(`document.querySelector(${JSON.stringify(selector)}).hidden`);
    const callsLog = () => (CALLS_LOG && fs.existsSync(CALLS_LOG)) ? fs.readFileSync(CALLS_LOG, "utf8") : "";

    await send("Page.enable");
    await send("Page.navigate", { url: URL });
    await until(`document.querySelectorAll("#tests-body tr[data-id]").length > 0`, 15000, "rows");
    check("table renders rows", (await rows()) === 5, `rows=${await rows()}`);
    check("admin: Run all visible", !(await hidden("#run-all")));

    // theme
    const before = await evaluate(`document.documentElement.dataset.theme || "auto"`);
    await click("#theme");
    const after = await evaluate(`document.documentElement.dataset.theme`);
    check("theme toggles", after && after !== before, `${before} -> ${after}`);
    await click("#theme");

    // filters
    await evaluate(`(() => { const s = document.getElementById("f-workflow"); s.value = "webshell"; s.dispatchEvent(new Event("change")); })()`);
    check("workflow filter", (await rows()) === 2, `rows=${await rows()}`);
    check("filter kept in URL", await evaluate(`location.hash.includes("workflow=webshell")`));
    await evaluate(`(() => { const s = document.getElementById("f-search"); s.value = "compute"; s.dispatchEvent(new Event("input")); })()`);
    await until(`document.querySelectorAll("#tests-body tr[data-id]").length === 1`, 3000, "search");
    check("search filter", (await rows()) === 1);
    await click("#f-clear");
    check("clear filters", (await rows()) === 5 && (await evaluate(`document.getElementById("f-search").value`)) === "");
    await evaluate(`(() => { const c = document.getElementById("f-changes"); c.checked = true; c.dispatchEvent(new Event("change")); })()`);
    check("changes only (none changed)", (await rows()) === 0, `rows=${await rows()}`);
    await click("#f-clear");

    // matrix chip opens the drawer, close, escape
    await click(".chip-btn[data-id]");
    check("matrix chip opens drawer", !(await hidden("#drawer")) && (await text("#d-title")).length > 0, await text("#d-title"));
    await click("#d-close");
    check("close button hides drawer", await hidden("#drawer"));
    await click("#tests-body tr[data-id]");
    check("row click opens drawer", !(await hidden("#drawer")));
    await evaluate(`document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))`);
    check("escape hides drawer", await hidden("#drawer"));

    // drawer tabs and artifact viewer
    const id = await evaluate(`document.querySelector("#tests-body tr[data-id]").dataset.id`);
    await click(`#tests-body tr[data-id="${id}"]`);
    await until(`document.getElementById("d-artifact").options.length > 0`, 10000, "artifact list");
    await until(`/test /.test(document.getElementById("d-log").textContent)`, 10000, "run.log content");
    check("log tab loads run.log", (await text("#d-log")).includes("test " + id));
    await click('.tab[data-tab="definition"]');
    await until(`document.getElementById("d-definition").textContent.includes("workflow_name")`, 5000, "definition");
    check("definition tab", !(await hidden('[data-panel="definition"]')) && (await hidden('[data-panel="history"]')));
    await click('.tab[data-tab="record"]');
    check("record tab", (await text("#d-record")).includes('"outcome"'));
    await click('.tab[data-tab="history"]');
    check("history tab rows", (await evaluate(`document.querySelectorAll("#d-history tr").length`)) >= 1);
    await evaluate(`(() => { const s = document.getElementById("d-file"); s.value = "launch.json"; s.dispatchEvent(new Event("change")); })()`);
    await until(`document.getElementById("d-log").textContent.includes('"slug"')`, 5000, "launch.json");
    check("artifact file select", true);

    // refresh pulls the bucket
    const callsBefore = callsLog().split("\n").filter((l) => l.includes("buckets cp -r")).length;
    await click("#refresh");
    await until(`document.getElementById("refresh").disabled === false`, 10000, "refresh done");
    const callsAfter = callsLog().split("\n").filter((l) => l.includes("buckets cp -r")).length;
    check("refresh pulls the bucket", callsAfter === callsBefore + 1, `pulls ${callsBefore} -> ${callsAfter}`);

    // rerun test from the drawer
    const countBefore = await evaluate(`Number(document.getElementById("d-facts").textContent.match(/Records(\\d+)/)?.[1] || 0)`);
    await click("#d-rerun");
    await until(`/started|failed/i.test(document.getElementById("d-admin-msg").textContent)`, 10000, "rerun message");
    check("rerun test starts", (await text("#d-admin-msg")).includes("Rerun started"), await text("#d-admin-msg"));
    await until(`document.querySelector("#d-chips .chip-running") !== null`, 20000, "running chip");
    check("running state shows", true);
    check("cancel run enabled while running", !(await evaluate(`document.getElementById("d-cancel").disabled`)));
    await click("#d-cancel");
    check("cancel asks for a second click", (await text("#d-cancel")).includes("Click again"), await text("#d-cancel"));
    await click("#d-cancel");
    await until(`/canceled|failed/i.test(document.getElementById("d-admin-msg").textContent)`, 10000, "cancel message");
    check("cancel run", (await text("#d-admin-msg")).includes("canceled"), await text("#d-admin-msg"));
    await until(`document.querySelector("#d-chips .chip-running") === null`, 60000, "run finished");
    const countAfter = await evaluate(`Number(document.getElementById("d-facts").textContent.match(/Records(\\d+)/)?.[1] || 0)`);
    check("rerun appended a record", countAfter === countBefore + 1, `${countBefore} -> ${countAfter}`);
    await click("#d-close");

    // delete the results of a test without a definition
    const old = "activate.parallel.works/alvaro/oldwf/oldtest";
    await click(`#tests-body tr[data-id="${old}"]`);
    await until(`!document.getElementById("d-delete").hidden`, 5000, "delete button");
    check("delete offered only for undefined tests", await evaluate(`document.getElementById("d-rerun").disabled`));
    await click("#d-delete");
    check("delete asks for a second click", (await text("#d-delete")).includes("Click again"), await text("#d-delete"));
    await click("#d-delete");
    try {
      await until(`!document.getElementById("notice").hidden && document.getElementById("notice").textContent.includes("deleted")`, 10000, "delete notice");
    } catch (error) {
      console.log("DEBUG admin message:", await text("#d-admin-msg"), "| notice:", await text("#notice"), "| button:", await text("#d-delete"), "hidden:", await hidden("#d-delete"), "disabled:", await evaluate(`document.getElementById("d-delete").disabled`));
      throw error;
    }
    await until(`document.querySelectorAll("#tests-body tr[data-id]").length === 4`, 10000, "row removed");
    check("results deleted from the bucket store", !fs.existsSync(CALLS_LOG.replace("calls.log", "bucket/alvaro/gcpbucket/probe/results/" + old)));

    // run all: two clicks, then a refused overlapping request
    await click("#run-all");
    check("run all asks for a second click", (await text("#run-all")).includes("Click again"), await text("#run-all"));
    await click("#run-all");
    await until(`/Suite run started|Could not start/.test(document.getElementById("notice").textContent)`, 10000, "run all notice");
    check("run all starts", (await text("#notice")).includes("Suite run started"), await text("#notice"));
    await sleep(500);
    await click("#run-all");
    await click("#run-all");
    await until(`document.getElementById("notice").textContent.includes("progress")`, 10000, "overlap refused");
    check("overlapping run all refused", (await text("#notice")).includes("already in progress"), await text("#notice"));
    await until(`document.querySelectorAll("#tests-body .chip-running").length === 0`, 90000, "suite finished");
    check("suite finished", true);
    ws.close();
  } finally {
    chrome.kill();
  }
  console.log(failures ? `${failures} check(s) failed` : "all checks passed");
  process.exit(failures ? 1 : 0);
}

main().catch((error) => { console.error("ERROR", error.message || error); process.exit(2); });
