from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web/src/composables/useWatchlistData.js"
DISPLAY = ROOT / "web/src/utils/watchlistDisplay.js"


class WatchlistFrontendTests(unittest.TestCase):
    def run_js(self, script):
        setup = f"""
import assert from 'node:assert/strict';
globalThis.window = {{setTimeout, clearTimeout, dispatchEvent() {{}}}};
globalThis.CustomEvent = class {{ constructor(name, fields) {{ Object.assign(this, fields); }} }};
const {{ useWatchlistData }} = await import({json.dumps(DATA.as_uri())});
const data = useWatchlistData();
const ok = payload => Promise.resolve({{ok:true, status:200, json:async () => payload}});
const board = (params, marker='selected') => ({{
  days: Number(params.get('days') || 5), stocks:[{{code:marker}}], sections:[], groups:[],
  total:1, total_all:3, ungrouped_count:1, trade_dates:[],
  generated_at:'2026-09-14T16:00:00+08:00', default_buy_date:'2026-09-14',
}});
"""
        result = subprocess.run(["node", "--input-type=module", "-e", setup + script],
                                cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_numbers_are_distinct_from_real_zero(self):
        self.run_js(f"""
const display = await import({json.dumps(DISPLAY.as_uri())});
for (const value of [null, undefined, '', '  ', NaN, Infinity, true, [], {{}}]) {{
  assert.equal(display.formatPrice(value), '—');
  assert.equal(display.formatMoney(value), '—');
  assert.equal(display.formatPct(value), '—');
  assert.equal(display.toneClass(value), '');
}}
assert.equal(display.formatPct(0), '0.00%');
assert.equal(display.formatPrice('0'), '0.00');
assert.equal(display.formatSignedMoney(-1), '-1.00');
assert.equal(display.dateLabel('2026-09-14'), '9.14 周一');
""")

    def test_actions_reload_current_filters_and_show_partial_failure(self):
        self.run_js("""
const reads = [];
globalThis.fetch = async (url, options) => {
  if (options.method === 'POST') return ok({added:['600002'], skipped_existing:['600001'],
    failed:[{code:'600002',error:'行情请求超时'}], board:{days:5, stocks:[{code:'wrong'}]}});
  const params = new URL(url, 'http://local').searchParams;
  reads.push(params);
  return ok(board(params));
};
Object.assign(data.state, {days:60, groupId:'ungrouped', q:'目标'});
await data.loadBoard();
const result = await data.addStocks({codes:'600001,600002'});
assert.equal(result.ok, true);
assert.equal(data.state.days, 60);
assert.equal(data.state.groupId, 'ungrouped');
assert.equal(data.state.q, '目标');
assert.equal(data.state.board.stocks[0].code, 'selected');
assert.equal(reads.at(-1).get('group_id'), 'ungrouped');
assert.equal(reads.at(-1).get('days'), '60');
assert.equal(reads.at(-1).get('q'), '目标');
assert.equal(data.state.actionFailures[0].code, '600002');
assert.match(data.state.status, /已有 1 只已跳过/);
assert.match(data.state.status, /1 只待补齐/);
""")

    def test_delayed_action_uses_new_filter_and_old_get_cannot_overwrite_it(self):
        self.run_js("""
let finishOld, finishAction;
globalThis.fetch = (url, options) => {
  if (options.method === 'PATCH') return new Promise(resolve => { finishAction = resolve; });
  const params = new URL(url, 'http://local').searchParams;
  if (params.get('q') === 'old') return new Promise(resolve => {finishOld = resolve;});
  return ok(board(params, params.get('q')));
};
const oldRead = data.setQuery('old');
const action = data.updateStock('600001', {note:'test'});
await data.setQuery('new');
finishAction(await ok({board:{days:5,stocks:[{code:'wrong'}]}}));
await action;
finishOld(await ok({days:5,stocks:[{code:'old'}]}));
await oldRead;
assert.equal(data.state.q, 'new');
assert.equal(data.state.board.stocks[0].code, 'new');
assert.equal(data.state.boardFilterKey, data.currentFilterKey());
assert.equal(data.state.loading, false);
""")

    def test_polling_restores_progress_retries_then_cleans_up(self):
        self.run_js("""
const timers = new Map(); let timerId = 0;
window.setTimeout = (fn, ms) => {timers.set(++timerId, {fn, ms}); return timerId;};
window.clearTimeout = id => timers.delete(id);
let poll = 0; const reads = []; const writes = [];
const first = {id:'first',kind:'backfill',status:'running',total:2,processed:1,succeeded:1,skipped:0,failed:[],retry_count:0};
globalThis.fetch = (url, options) => {
  if (options.method === 'POST') {
    writes.push(url);
    return ok({job:{...first,id:'retry',total:1,processed:0,succeeded:0}});
  }
  if (url.endsWith('/jobs/latest')) {
    poll += 1;
    return ok({job:poll === 1 ? first : {...first,status:'partial',processed:2,
      failed:[{code:'600002',error:'超时'}],retry_count:1}});
  }
  const params = new URL(url, 'http://local').searchParams;
  reads.push(params);
  return ok(board(params));
};
Object.assign(data.state, {days:15,groupId:'ungrouped',q:'目标'});
await data.startJobPolling();
assert.equal(data.state.job.status, 'running');
const scheduled = [...timers.entries()].find(([, timer]) => timer.ms === 2000);
assert.ok(scheduled);
timers.delete(scheduled[0]); await scheduled[1].fn();
assert.equal(data.state.job.status, 'partial');
assert.equal(data.state.job.failed[0].code, '600002');
assert.equal(reads.at(-1).get('q'), '目标');
await data.retryJob();
assert.equal(writes[0], '/api/watchlist/jobs/first/retry');
assert.equal(data.state.job.id, 'retry');
assert.equal(data.state.job.total, 1);
data.stopJobPolling();
assert.equal(timers.size, 0);
""")

    def test_late_poll_cannot_restart_after_component_unmount(self):
        self.run_js("""
const timers = new Map(); let id = 0; let finish;
window.setTimeout = (fn, ms) => {timers.set(++id, {fn,ms}); return id;};
window.clearTimeout = id => timers.delete(id);
globalThis.fetch = () => new Promise(resolve => {finish = resolve;});
const pending = data.startJobPolling();
data.stopJobPolling();
finish(await ok({job:{id:'late',status:'running',total:1,processed:0}}));
await pending;
assert.equal(data.state.job, null);
assert.equal(timers.size, 0);
""")

    def test_load_failure_keeps_snapshot_and_separates_error_from_empty_results(self):
        self.run_js("""
globalThis.fetch = url => ok(board(new URL(url,'http://local').searchParams));
await data.loadBoard();
globalThis.fetch = async () => {throw new Error('connection lost');};
assert.equal(await data.loadBoard(), false);
assert.equal(data.state.board.stocks[0].code, 'selected');
assert.equal(data.state.loadError, 'connection lost');
await data.setQuery('new');
assert.notEqual(data.state.boardFilterKey, data.currentFilterKey());
""")

    def test_new_notices_wrap_and_keep_mobile_touch_targets(self):
        css = (ROOT / "frontend/watchlist.css").read_text(encoding="utf-8")
        panel = (ROOT / "web/src/components/WatchlistPanel.vue").read_text(encoding="utf-8")
        self.assertRegex(css, r"(?s)\.watchlist-job \{[^}]*min-width:0;[^}]*max-width:100%;[^}]*overflow-wrap:anywhere;")
        self.assertRegex(css, r"(?s)@media \(max-width:720px\).*?\.watchlist-job button,.*?min-height:44px;")
        self.assertIn("flex-wrap:wrap", css)
        self.assertIn("没有符合当前分组或搜索条件的股票", panel)
        self.assertIn("当前筛选结果加载失败", panel)
        self.assertIn("onUnmounted", panel)
        self.assertIn("value=\"ungrouped\"", panel)
