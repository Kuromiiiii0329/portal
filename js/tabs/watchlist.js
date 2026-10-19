/**
 * tabs/watchlist.js — 选股管理
 *   - 每项 checkbox：勾选参与"分析勾选自选股"批量任务，状态持久化
 *   - 显示股票名称（store 持久化）优先，无名称回退代码
 *   - 添加时若本地服务在线，自动查 /quote 拿名称写回 store
 *   - 实时行情（价格/涨跌幅）在 server 在线时叠加显示
 */
import { SERVER } from '../config.js';

export class WatchlistTab {
  constructor(container, store, toast) {
    this._c = container; this._s = store; this._t = toast;
    this._online = false;
    this._quotes = {};   // { "600519": { name, price, pct_chg, volume } }
  }

  init() {
    this._c.innerHTML = `
      <div class="max-w-2xl mx-auto space-y-5 pb-20">
        <div>
          <h2 class="text-base font-semibold text-gray-900">自选股管理</h2>
          <p class="text-xs text-gray-500 mt-0.5">支持 A股（600519）、港股（00700）、美股（AAPL）· 勾选项可批量分析</p>
        </div>

        <!-- 输入区 -->
        <div class="flex gap-2">
          <input id="wl-input" type="text" placeholder="输入股票代码，如 600519" maxlength="12"
            class="form-input flex-1" autocomplete="off" />
          <button id="wl-add"
            class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium rounded-lg transition-colors">
            添加
          </button>
        </div>

        <!-- 全选 / 反选工具条 -->
        <div id="wl-toolbar" class="hidden items-center gap-3 text-xs text-gray-500">
          <label class="flex items-center gap-1.5 cursor-pointer select-none">
            <input id="wl-check-all" type="checkbox" class="w-4 h-4 accent-blue-600 cursor-pointer" />
            全选
          </label>
          <span id="wl-checked-count" class="text-gray-400"></span>
        </div>

        <!-- 列表 -->
        <div id="wl-list" class="space-y-1.5"></div>
        <p id="wl-count" class="text-xs text-gray-400"></p>
      </div>`;

    const input = this._c.querySelector('#wl-input');
    this._c.querySelector('#wl-add').addEventListener('click', () => this._add(input));
    input.addEventListener('keydown', e => { if (e.key === 'Enter') this._add(input); });
    this._c.querySelector('#wl-check-all').addEventListener('change', e => {
      this._s.setAllChecked(e.target.checked);
    });
    this._s.subscribe(state => this._render(state));

    // 初始尝试加载行情
    this._loadQuotes();
  }

  /** 由 app 层在服务器状态变更时调用 */
  setServerStatus(online) {
    this._online = online;
    if (online) this._loadQuotes();
  }

  async _loadQuotes() {
    const state = this._s.get();
    const stocks = (state && state.stock_list) ? state.stock_list : [];
    if (!stocks.length) return;
    // 不依赖 _online 标志（可能未同步）；直接尝试，失败静默

    try {
      const codes = stocks.map(s => s.code).join(',');
      const res = await fetch(`${SERVER}/quote?codes=${encodeURIComponent(codes)}`);
      if (!res.ok) return;
      const data = await res.json();
      if (!data.ok || !Array.isArray(data.quotes)) return;

      const map = {};
      for (const q of data.quotes) {
        map[q.code] = { name: q.name, price: q.price, pct_chg: q.pct_chg, volume: q.volume };
        // 行情带回名称 → 持久化到 store（若该股尚无名称）
        if (q.name) this._s.setStockName(q.code, q.name);
      }
      this._quotes = map;
      this._render(this._s.get());
    } catch (_) { /* 网络失败静默 */ }
  }

  _add(input) {
    const raw = input.value;
    const r = this._s.addStock(raw);
    if (r.ok) {
      const code = raw.trim().toUpperCase();
      input.value = '';
      this._t.show('已添加', 'success');
      // server 在线：立即查该股名称 + 行情
      this._fetchName(code);
      this._loadQuotes();
    } else {
      this._t.show(r.msg, 'warning');
    }
  }

  /** 添加时查单只股票名称，写回 store 持久化 */
  async _fetchName(code) {
    // 不依赖 _online（懒加载 Tab 可能未收到 setServerStatus）；直接试，失败静默
    try {
      const res = await fetch(`${SERVER}/quote?codes=${encodeURIComponent(code)}`);
      if (!res.ok) return;
      const data = await res.json();
      const q = (data.quotes || [])[0];
      if (q && q.name) this._s.setStockName(code, q.name);
    } catch (_) {}
  }

  _render(state) {
    const list  = this._c.querySelector('#wl-list');
    const count = this._c.querySelector('#wl-count');
    const toolbar = this._c.querySelector('#wl-toolbar');
    if (!list) return;
    const stocks = state.stock_list || [];
    if (!stocks.length) {
      list.innerHTML = `
        <div class="py-12 text-center text-gray-300">
          <div class="text-4xl mb-2">📋</div>
          <p class="text-sm">还没有自选股</p>
        </div>`;
      if (count) count.textContent = '';
      if (toolbar) toolbar.classList.add('hidden');
      return;
    }

    if (toolbar) toolbar.classList.remove('hidden');

    list.innerHTML = stocks.map((s, i) => {
      const code = s.code;
      const q = this._quotes[code] || {};
      // 显示名优先级：store 持久化名 > 行情名 > 代码
      const displayName = s.name || q.name || '';
      const nameHtml = displayName
        ? `<span class="font-semibold text-sm text-gray-800">${this._esc(displayName)}</span>
           <span class="ml-1.5 font-mono text-xs text-gray-400">${this._esc(code)}</span>`
        : `<span class="font-mono font-semibold text-sm text-gray-800">${this._esc(code)}</span>`;

      let priceHtml = '';
      if (q.price != null) {
        priceHtml = `<span class="text-xs text-gray-600 font-mono">${q.price.toFixed(2)}</span>`;
      }
      let pctHtml = '';
      if (q.pct_chg != null) {
        const sign  = q.pct_chg >= 0 ? '+' : '';
        const color = q.pct_chg > 0 ? 'text-green-500' : q.pct_chg < 0 ? 'text-red-500' : 'text-gray-400';
        pctHtml = `<span class="text-xs font-semibold ${color}">${sign}${q.pct_chg.toFixed(2)}%</span>`;
      }
      const hasQuote = priceHtml || pctHtml;

      return `
        <div class="flex items-center justify-between px-4 py-2.5 bg-white rounded-xl border border-gray-100
                    hover:border-blue-200 hover:shadow-sm transition-all group">
          <div class="flex items-center gap-3 min-w-0">
            <input type="checkbox" data-check="${this._esc(code)}" ${s.checked ? 'checked' : ''}
              class="w-4 h-4 accent-blue-600 cursor-pointer shrink-0" />
            <span class="text-xs text-gray-300 w-4 text-right shrink-0">${i + 1}</span>
            <div class="flex flex-col min-w-0">
              <div class="flex items-center gap-1.5">${nameHtml}</div>
              ${hasQuote ? `<div class="flex items-center gap-1.5 mt-0.5">${priceHtml}${pctHtml}</div>` : ''}
            </div>
          </div>
          <button data-del="${this._esc(code)}"
            class="text-gray-200 hover:text-red-400 transition-colors text-lg leading-none opacity-0 group-hover:opacity-100 shrink-0">×</button>
        </div>`;
    }).join('');

    // 绑定 checkbox
    list.querySelectorAll('[data-check]').forEach(cb =>
      cb.addEventListener('change', () => this._s.toggleChecked(cb.dataset.check, cb.checked)));
    // 绑定删除
    list.querySelectorAll('[data-del]').forEach(b =>
      b.addEventListener('click', () => {
        this._s.removeStock(b.dataset.del);
        this._t.show('已移除', 'info');
      }));

    // 更新计数 + 全选态
    const checkedN = stocks.filter(s => s.checked).length;
    if (count) count.textContent = `共 ${stocks.length} 只`;
    const cntEl = this._c.querySelector('#wl-checked-count');
    if (cntEl) cntEl.textContent = `已勾选 ${checkedN} 只`;
    const allEl = this._c.querySelector('#wl-check-all');
    if (allEl) {
      allEl.checked = checkedN === stocks.length && stocks.length > 0;
      allEl.indeterminate = checkedN > 0 && checkedN < stocks.length;
    }
  }

  _esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
}
