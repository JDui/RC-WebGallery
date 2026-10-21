const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appSourcePath = path.join(__dirname, "..", "app", "static", "app.js");
const appSource = fs.readFileSync(appSourcePath, "utf8");

function createApp() {
  const values = new Map();
  const localStorage = {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
  };
  const context = vm.createContext({
    localStorage,
    window: { innerWidth: 1280, location: { reload() {} } },
    document: { title: "test" },
    console,
    setTimeout,
    clearTimeout,
  });
  vm.runInContext(appSource, context, { filename: appSourcePath });
  return context.galleryApp();
}

test("legacy 9/8/1 follow-up remains local and keeps repair pointed at its source task", async () => {
  const app = createApp();
  const oldCompletedTask = {
    id: 41,
    task_type: "livephoto-repair",
    status: "partial",
    finished_at: "2026-10-21T10:00:00Z",
    details: {
      source_task_id: 37,
      scope_folders: ["bili-old-folder"],
      checked: 9,
      paired: 8,
      repairable: 1,
      repairable_targets: [{ folder_name: "bili-old-folder", pair_index: 4 }],
      exception_targets: [],
    },
  };
  const unfinishedTask = {
    id: 42,
    task_type: "livephoto-check",
    status: "running",
    details: { completed: false, checked: 0, paired: 0 },
  };
  app.taskRuns = [unfinishedTask, oldCompletedTask];
  app.queuedTasks = [];
  app.pullStatus = { mode: "idle", running: false, queue: [] };

  const result = app.livephotoLatestResult();
  assert.equal(result.id, oldCompletedTask.id);
  assert.equal(result.subset, true);
  assert.equal(result.checked, 9);
  assert.equal(result.paired, 8);
  assert.equal(result.repairable, 1);
  assert.match(app.livephotoResultText(), /局部复查/);
  assert.doesNotMatch(app.livephotoResultText(), /全库检查/);
  assert.equal(app.livephotoTaskActive(), true, "the newer running task should still gate the buttons");
  assert.equal(app.livephotoRepairVisible(), false);

  app.taskRuns = [oldCompletedTask];
  assert.equal(app.livephotoRepairVisible(), true);
  const requests = [];
  app.api = async (url) => { requests.push(url); return { message: "queued" }; };
  app.refreshStatus = async () => {};
  app.refreshTasks = async () => {};
  app.notify = () => {};
  await app.submitLivephotoFollowup("repair");
  assert.deepEqual(requests, ["/api/settings/livephoto/41/followup/repair"]);
});

test("all-scope result is labeled as a full-library check and help distinguishes the counts", () => {
  const app = createApp();
  app.taskRuns = [{
    id: 55,
    task_type: "livephoto-check",
    status: "success",
    details: {
      scan_scope: "all",
      completed: true,
      checked: 175,
      paired: 170,
      repairable: 5,
      repairable_targets: Array.from({ length: 5 }, (_, index) => ({ pair_index: index + 1 })),
    },
  }];
  app.queuedTasks = [];
  app.pullStatus = { mode: "idle", running: false, queue: [] };
  assert.equal(app.livephotoLatestResult().subset, false);
  assert.match(app.livephotoResultText(), /全库检查/);

  const html = fs.readFileSync(path.join(__dirname, "..", "app", "templates", "index.html"), "utf8");
  assert.match(html, /data-testid="livephoto-check"/);
  const help = html.match(/data-tooltip="([^"]*含实况的动态数[^"]*)"/);
  assert.ok(help, "the Live Photo count tooltip should be present");
  assert.match(help[1], /检查全库/);
  assert.match(help[1], /局部复查只检查上次有问题的内容，不代表全库数量/);
});
