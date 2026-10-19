/**
 * tabs/virtual.js — 虚拟盘 Tab
 *
 * 只做三件事：**看清状态 / 跑一次动作 / 打开报告**。
 * 所有数字都来自 `srv/virtual_api.py`，前端不做任何计算 ——
 * 前端一旦开始“算”，就会出现第二套口径，而口径分叉本身就是静默错误的来源。
 */
import { SERVER } from '../config.js';

const STATUS_POLL_MS = 8000;

export class VirtualTab {
  constructor(container, store, toast) {
    this._c = container;
    this._s = store;
    this._t = toast;
    this._online = false;
    this._timer = null;
    this._state = null;
    this._busy = '';
  }

  init() {
    this._c.innerHTML = `
      <div class="max-w-4xl mx-auto space-y-4">
        <div class="flex items-start justify-between gap-3">
          <div>
            <h2 class="text-base font-semibold text-gray-900">虚拟盘</h2>
            <p class="text-xs text-gray-500 mt-0.5">
              计划 → 机械回放结算 → 账本 → 规则闸门。所有收益由确定性内核计算。
            </p>
          </div>
          <button id="vtab-refresh"
            class="shrink-0 text-xs px-3 py-1.5 rounded-lg border border-gray-200
                   bg-white hover:bg-gray-50 text-gray-700">刷新</button>
        </div>

        <div id="vtab-banner"></div>
        <div id="vtab-kpis" class="grid grid-cols-2 md:grid-cols-4 gap-2"></div>

        <div class="bg-white border border-gray-100 rounded-2xl shadow-sm p-3">
          <p class="text-xs font-semibold text-gray-700 mb-2">动作</p>
          <div class="flex flex-wrap gap-2">
            <button data-act="warmup"
              class="text-xs px-3 py-1.5 rounded-lg bg-blue-600 text-white hover:bg-blue-700">
              预热近 20 日特征</button>
            <button data-act="evolve"
              class="text-xs px-3 py-1.5 rounded-lg bg-violet-600 text-white hover:bg-violet-700">
              跑一轮演化（不落库）</button>
            <button data-act="report"
              class="text-xs px-3 py-1.5 rounded-lg bg-emerald-600 text-white hover:bg-emerald-700">
              生成复盘报告</button>
          </div>
          <pre id="vtab-action-log"
            class="mt-2 text-[11px] leading-relaxed text-gray-600 bg-gray-50 rounded-lg p-2
                   max-h-40 overflow-auto hidden"></pre>
        </div>

        <div class="bg-white border border-gray-100 rounded-2xl shadow-sm p-3">
          <div class="flex items-center justify-between mb-2">
            <p class="text-xs font-semibold text-gray-700">规则库</p>
            <span id="vtab-rules-count" class="text-[11px] text-gray-400"></span>
          </div>
          <div id="vtab-rules" class="text-xs text-gray-500">加载中…</div>
        </div>

        <div class="bg-white border border-gray-100 rounded-2xl shadow-sm p-3">
          <p class="text-xs font-semibold text-gray-700 mb-2">预测校准</p>
          <div id="vtab-calib" class="text-xs text-gray-500">加载中…</div>
        </div>

        <div class="bg-white border border-gray-100 rounded-2xl shadow-sm p-3">
          <p class="text-xs font-semibold text-gray-700 mb-2">账本</p>
          <pre id="vtab-ledger"
            class="text-[11px] leading-relaxed whitespace-pre-wrap text-gray-600
                   max-h-56 overflow-auto">加载中…</pre>
        </div>
      </div>`;

    this._c.querySelector('#vtab-refresh')
      .addEventListener('click', () => this.refresh());
    this._c.querySelectorAll('[data-act]').forEach(b => {
      b.addEventListener('click', () => this._action(b.dataset.act, b));
    });
  }

  setServerStatus(online) {
    this._online = online;
    if (online) {
      this.refresh();
      if (!this._timer) {
        this._timer = setInterval(() => this.refresh(), STATUS_POLL_MS);
      }
    } else if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
      this._banner('离线模式：虚拟盘需要本地服务（点「启动本地服务」）。', 'warn');
      this._kpis([]);
    }
  }

  async refresh() {
    if (!this._online) return;
    try {
      const r = await fetch(`${SERVER}/virtual/status`);
      const data = await r.json();
      this._state = data;
      this._render(data);
    } catch (e) {
      this._banner(`读取状态失败：${e}`, 'err');
    }
  }

  // ── 渲染 ─────────────────────────────────────────────
  _render(d) {
    if (!d || d.ok === false) {
      this._banner(`后端报错：${(d && d.error) || '未知'}`, 'err');
      return;
    }
    const db = d.db || {};
    if (!db.available) {
      this._banner(
        `回测数据库不可用：${db.reason || '未构建'}。` +
        `在终端运行 <code>python -m portal.virtual.marketdb build</code> 后刷新。`, 'warn');
    } else {
      this._banner(
        `数据库：${db.codes_with_data}/${db.stocks} 只，${this._num(db.rows)} 行，` +
        `${db.date_min || '—'} ~ ${db.date_max || '—'}`, 'ok');
    }

    const rules = d.rules || {};
    const preds = d.predictions || {};
    const cal = preds.calibration || {};
    this._kpis([
      { k: '规则总数', v: rules.count ?? 0 },
      { k: '已活化', v: (rules.by_status && rules.by_status.active) || 0 },
      { k: '预测样本', v: cal.n ?? 0 },
      { k: '特征天数', v: (d.features && d.features.days) || 0 },
    ]);

    this._renderRules(rules);
    this._renderCalib(cal, preds);
    this._loadLedger();
  }

  _kpis(items) {
    const box = this._c.querySelector('#vtab-kpis');
    if (!items.length) { box.innerHTML = ''; return; }
    box.innerHTML = items.map(i => `
      <div class="bg-white border border-gray-100 rounded-xl shadow-sm px-3 py-2">
        <p class="text-[11px] text-gray-400">${i.k}</p>
        <p class="text-lg font-semibold text-gray-800">${i.v}</p>
      </div>`).join('');
  }

  _renderRules(rules) {
    const box = this._c.querySelector('#vtab-rules');
    const count = this._c.querySelector('#vtab-rules-count');
    const rows = rules.rows || [];
    if (count) count.textContent = `${rows.length} 条`;
    if (!rows.length) {
      box.innerHTML = '<span class="text-gray-400">规则库为空。运行 ' +
        '<code>python -m portal.virtual.rules bootstrap</code> 导入种子规则。</span>';
      return;
    }
    const pill = s => {
      const map = {
        active: 'bg-green-100 text-green-700', testing: 'bg-blue-100 text-blue-700',
        hypothesis: 'bg-gray-100 text-gray-600', decaying: 'bg-amber-100 text-amber-700',
        retired: 'bg-red-100 text-red-600',
      };
      return `<span class="px-1.5 py-0.5 rounded ${map[s] || 'bg-gray-100 text-gray-600'}">${s}</span>`;
    };
    const num = v => (v === null || v === undefined) ? '—' : v;
    box.innerHTML = `
      <table class="w-full text-[11px]">
        <thead><tr class="text-gray-400">
          <th class="text-left font-normal py-1">规则</th>
          <th class="text-left font-normal">状态</th>
          <th class="text-right font-normal">n</th>
          <th class="text-right font-normal">均值%</th>
          <th class="text-right font-normal">扣费后%</th>
        </tr></thead>
        <tbody>${rows.map(r => `
          <tr class="border-t border-gray-50">
            <td class="py-1">
              <span class="font-mono text-gray-700">${r.rule_id}</span>
              <div class="text-gray-400">${r.desc || ''}</div>
              ${(r.flags || []).map(f => `<div class="text-amber-600">⚠ ${f}</div>`).join('')}
            </td>
            <td>${pill(r.status)}</td>
            <td class="text-right">${num(r.n)}</td>
            <td class="text-right ${(r.mean_pct || 0) > 0 ? 'text-red-600' : 'text-green-700'}">${num(r.mean_pct)}</td>
            <td class="text-right ${(r.after_cost_pct || 0) > 0 ? 'text-red-600' : 'text-green-700'}">${num(r.after_cost_pct)}</td>
          </tr>`).join('')}</tbody>
      </table>`;
  }

  _renderCalib(cal, preds) {
    const box = this._c.querySelector('#vtab-calib');
    if (!cal || !cal.n) {
      box.innerHTML = `<span class="text-gray-400">还没有已平仓样本（${cal && cal.note ? cal.note : '—'}）。` +
        ` 需要先跑一轮 plan → settle → exit。</span>`;
      return;
    }
    const rows = [
      ['样本数', cal.n],
      ['命中率', `${cal.hit_rate}%`],
      ['平均已实现', `${cal.avg_realized_pct}%`],
      ['中位', `${cal.median_realized_pct}%`],
      ['Brier', cal.brier === null ? '—（无概率字段）' : cal.brier],
      ['forced_hold', cal.forced_hold_n],
      ['Z1 顺延', cal.z1_defer_n],
    ];
    box.innerHTML = `
      <div class="grid grid-cols-2 md:grid-cols-4 gap-2">
        ${rows.map(([k, v]) => `
          <div class="bg-gray-50 rounded-lg px-2 py-1.5">
            <p class="text-[10px] text-gray-400">${k}</p>
            <p class="text-sm text-gray-800">${v}</p>
          </div>`).join('')}
      </div>
      <p class="mt-2 text-[11px] ${cal.n < 30 ? 'text-amber-600' : 'text-gray-400'}">
        ${cal.sample_note || ''}</p>`;
  }

  async _loadLedger() {
    try {
      const r = await fetch(`${SERVER}/virtual/ledger`);
      const d = await r.json();
      const el = this._c.querySelector('#vtab-ledger');
      el.textContent = (d && d.text) ? d.text :
        (d.note || '（还没有账本：先跑一次买入日结算）');
    } catch (e) { /* 账本读失败不影响其他区块 */ }
  }

  // ── 动作 ─────────────────────────────────────────────
  async _action(act, btn) {
    if (!this._online) { this._banner('需要本地服务在线才能执行动作。', 'warn'); return; }
    if (this._busy) return;
    this._busy = act;
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = '执行中…';
    const log = this._c.querySelector('#vtab-action-log');
    log.classList.remove('hidden');
    log.textContent = `[${act}] 请求已发出，请稍候…`;
    try {
      const ep = act === 'report' ? '/virtual/report'
        : act === 'warmup' ? '/virtual/warmup' : '/virtual/evolve';
      const body = act === 'warmup' ? { days: 20 }
        : act === 'evolve' ? { max_days: 40, commit: false } : { open: true };
      const r = await fetch(`${SERVER}${ep}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      log.textContent = JSON.stringify(d, null, 2);
      if (this._t) {
        if (d.ok) this._t.success(`${act} 完成`);
        else this._t.error(`${act} 失败：${d.error || '未知'}`);
      }
      await this.refresh();
    } catch (e) {
      log.textContent = `${act} 失败：${e}`;
      if (this._t) this._t.error(`${act} 失败：${e}`);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
      this._busy = '';
    }
  }

  // ── 小工具 ───────────────────────────────────────────
  _banner(text, kind) {
    const el = this._c.querySelector('#vtab-banner');
    const cls = kind === 'err' ? 'bg-red-50 border-red-100 text-red-700'
      : kind === 'warn' ? 'bg-amber-50 border-amber-100 text-amber-700'
        : 'bg-emerald-50 border-emerald-100 text-emerald-700';
    el.innerHTML = `<div class="border rounded-xl px-3 py-2 text-xs ${cls}">${text}</div>`;
  }

  _num(n) {
    if (n === null || n === undefined) return '—';
    return Number(n).toLocaleString('zh-CN');
  }
}
