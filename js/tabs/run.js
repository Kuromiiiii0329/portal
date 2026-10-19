/**
 * tabs/run.js — 立即运行分析（三子面板布局）
 *
 * 子面板布局：
 *   [分析] 紧凑输入 + 自选股芯片 + 可折叠维度选择（<details>）+ 操作按钮行
 *   [日志] 状态 badge + 清空按钮 + 全高日志区（每行带 [HH:MM:SS] 时间戳）
 *   [报告] 工具栏（复制/视图切换）+ 报告内容区 + 历史列表（最近 5 条）
 *
 * 流程：
 *   点击分析 -> _deepAnalyze() -> _switchSubTab('log') -> SSE done -> _switchSubTab('report')
 *
 * 历史记录：
 *   localStorage key: "dsa_run_history"
 *   格式：[{code, name, score, signal, time, taskId}]
 *   最多 10 条，新的在前
 */

import { ReportView } from '../components/report-view.js';
import { SERVER } from '../config.js';

const DIM_DEFS = {
  technical: {
    label: '📊 技术面', color: 'blue',
    mods: {
      ma_system: '均线系统 MA5/10/20/60', macd: 'MACD',
      rsi: 'RSI', kdj: 'KDJ', bollinger: '布林带',
      overbought: '超买超卖综合', divergence: '背离(顶/底)',
      volume: '量价关系', pattern: 'K线形态(LLM)',
      wave: '波浪理论(LLM)', chan: '缠论(LLM)',
    },
    defaults: ['ma_system','macd','rsi','kdj','bollinger','overbought','divergence','volume'],
  },
  fundamental: {
    label: '📈 基本面', color: 'emerald',
    mods: {
      financials: '核心财报', growth: '成长能力',
      dividend: '分红质量', capital_flow: '主力资金',
      valuation: '估值水平（PE/PB）', business: '主营业务(LLM)',
    },
    defaults: ['financials','growth','valuation','capital_flow'],
  },
  industry: {
    label: '🏭 产业链', color: 'violet',
    mods: {
      key_commodity: '核心商品/产品价格',
      industry_chain: '产业链地位',
      competitors: '竞争格局',
      policy: '政策风向',
    },
    defaults: ['key_commodity','industry_chain','competitors','policy'],
  },
};

const HISTORY_KEY = 'dsa_run_history';
const HISTORY_MAX = 10;
const HISTORY_SHOW = 5;
const REPORT_CACHE_PREFIX = 'dsa_report_';

export class RunTab {
  constructor(container, store, toast) {
    this._c = container; this._s = store; this._t = toast;
    this._online         = false;
    this._sse            = null;
    this._report         = null;
    this._selDims        = new Set(['technical','fundamental','industry']);
    this._selMods        = Object.fromEntries(
      // 默认全选：每个维度的全部子模块都勾选
      Object.entries(DIM_DEFS).map(([k,v]) => [k, new Set(Object.keys(v.mods))])
    );
    this._serverStarting = false;
    this._activeSub      = 'analyze'; // 'analyze' | 'log' | 'report'
    this._viewMode       = 'structured'; // 'structured' | 'markdown'
    this._lastRawReport  = null;
    this._lastRtype      = null;
  }

  init() {
    this._render();
    this._s.subscribe(st => this._syncChips(st));
  }

  setServerStatus(online) {
    const changed = online !== this._online;
    this._online = online;
    if (changed) this._updateOnlineUI();
  }

  // ── 主渲染 ────────────────────────────────────────────────────────────
  _render() {
    this._c.innerHTML = `
      <div class="max-w-3xl mx-auto pb-20">

        <!-- 页头：标题 + 服务状态 -->
        <div class="flex items-center justify-between mb-4">
          <div>
            <h2 class="text-base font-semibold text-gray-900">立即运行分析</h2>
            <p class="text-xs text-gray-500 mt-0.5">深度双维度分析 · 或快速触发大盘/全量任务</p>
          </div>
          <div id="run-svc-chip"
            class="flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs font-medium
                   bg-gray-50 border-gray-200 text-gray-400">
            <span class="status-dot offline" id="run-dot"></span>
            <span id="run-svc-label">纯静态模式</span>
          </div>
        </div>

        <!-- 子 Tab 导航栏 -->
        <div class="flex gap-1 mb-4 bg-gray-100 rounded-xl p-1">
          <button data-sub="analyze"
            class="sub-tab flex-1 py-2 text-xs font-semibold rounded-lg transition-all">
            ⚙️ 分析
          </button>
          <button data-sub="log"
            class="sub-tab flex-1 py-2 text-xs font-semibold rounded-lg transition-all">
            <span>📋 日志</span>
            <span id="tab-log-badge" class="hidden ml-1 inline-block w-2 h-2 rounded-full bg-blue-500 align-middle"></span>
          </button>
          <button data-sub="report"
            class="sub-tab flex-1 py-2 text-xs font-semibold rounded-lg transition-all">
            <span>📄 报告</span>
            <span id="tab-report-badge" class="hidden ml-1 inline-block w-2 h-2 rounded-full bg-green-500 align-middle"></span>
          </button>
        </div>

        <!-- ══ 子面板 1：分析 ══ -->
        <div id="sub-analyze" class="space-y-4">

          <div class="bg-white border border-gray-100 rounded-2xl shadow-sm overflow-hidden">
            <div class="px-4 pt-3 pb-2.5 border-b border-gray-50 flex items-center gap-2">
              <p class="text-sm font-semibold text-gray-800">🔬 深度分析</p>
              <span class="text-xs text-gray-400">技术面 + 基本面 + 产业链</span>
            </div>
            <div class="px-4 py-3 space-y-3">

              <!-- 输入行：股票代码 + 股票名称（同一行 flex） -->
              <div class="flex gap-2">
                <input id="run-code" type="text" placeholder="股票代码，如 002466" maxlength="12"
                  class="form-input flex-1 text-sm" autocomplete="off" />
                <input id="run-name" type="text" placeholder="名称（可选）" maxlength="20"
                  class="form-input w-32 text-sm" />
              </div>

              <!-- 自选股芯片 -->
              <div id="run-chips" class="flex flex-wrap gap-1.5 min-h-[1.5rem]"></div>

              <!-- 维度选择：<details> 折叠，默认收起 -->
              <details class="group">
                <summary class="flex items-center gap-1.5 text-xs text-gray-500
                                hover:text-gray-700 transition-colors cursor-pointer select-none list-none">
                  <span class="transition-transform group-open:rotate-90">▶</span>
                  <span>高级选项（维度）已选 <span id="dims-count">3</span> 个维度</span>
                </summary>
                <div id="dim-list" class="mt-2 space-y-2"></div>
              </details>

              <!-- 🤖 Agent 综合研判开关（选项B：基于已算好的指标做深度研判）-->
              <label class="flex items-center gap-2 mb-2 px-1 cursor-pointer select-none">
                <input type="checkbox" id="chk-agent-review"
                  class="w-3.5 h-3.5 accent-blue-600"/>
                <span class="text-xs text-gray-600">🤖 深度分析后附加 <b>Agent 综合研判</b>（复用已算指标，多花约 1 次 LLM）</span>
              </label>

              <!-- 操作按钮行：[深度分析] [大盘复盘] [全量分析] -->
              <div class="flex gap-2">
                <button id="btn-deep"
                  class="flex-1 py-2.5 rounded-xl text-xs font-bold transition-all
                         bg-blue-600 hover:bg-blue-700 text-white shadow-sm shadow-blue-200
                         active:scale-[.99] disabled:opacity-50 disabled:cursor-not-allowed">
                  🔬 深度分析
                </button>
                <button id="btn-market"
                  class="flex-1 py-2.5 rounded-xl text-xs font-bold transition-all
                         bg-indigo-600 hover:bg-indigo-700 text-white shadow-sm shadow-indigo-200
                         active:scale-[.99] disabled:opacity-50 disabled:cursor-not-allowed">
                  🌐 大盘复盘
                </button>
                <button id="btn-full"
                  class="flex-1 py-2.5 rounded-xl text-xs font-bold transition-all
                         bg-purple-600 hover:bg-purple-700 text-white shadow-sm shadow-purple-200
                         active:scale-[.99] disabled:opacity-50 disabled:cursor-not-allowed">
                  ⚡ 全量分析
                </button>
              </div>

              <!-- 批量：分析勾选的自选股 -->
              <button id="btn-batch"
                class="w-full py-2.5 rounded-xl text-xs font-bold transition-all
                       bg-teal-600 hover:bg-teal-700 text-white shadow-sm shadow-teal-200
                       active:scale-[.99] disabled:opacity-50 disabled:cursor-not-allowed">
                📑 批量分析勾选的自选股
              </button>
              <p class="text-xs text-gray-400 -mt-1">按顺序依次深度分析「自选股」Tab 中勾选的股票，逐个生成报告</p>

            </div>
          </div>

          <!-- 回测卡片 -->
          <div class="bg-white border border-gray-100 rounded-2xl shadow-sm overflow-hidden">
            <div class="px-4 pt-3 pb-2.5 border-b border-gray-50 flex items-center justify-between">
              <div class="flex items-center gap-2">
                <p class="text-sm font-semibold text-gray-800">📈 信号回测</p>
                <span class="text-xs text-gray-400">基于 K 线历史验证各技术信号胜率</span>
              </div>
              <span id="bt-note" class="text-xs text-gray-300">需先运行一次深度分析建立缓存</span>
            </div>
            <div class="px-4 py-3 space-y-3">
              <div class="flex gap-2">
                <input id="bt-code" type="text" placeholder="股票代码，如 002466" maxlength="12"
                  class="form-input flex-1 text-sm" autocomplete="off" />
                <button id="btn-backtest"
                  class="px-4 py-2 rounded-xl text-xs font-bold transition-all
                         bg-emerald-600 hover:bg-emerald-700 text-white shadow-sm shadow-emerald-200
                         active:scale-[.99] disabled:opacity-50 disabled:cursor-not-allowed">
                  运行回测
                </button>
              </div>
              <!-- 结果区：默认隐藏 -->
              <div id="bt-result" class="hidden space-y-3">
                <div id="bt-meta" class="text-xs text-gray-400"></div>
                <div id="bt-chart" style="height:300px"></div>
                <div id="bt-table" class="overflow-x-auto"></div>
              </div>
              <div id="bt-error" class="hidden text-xs text-red-400 py-2"></div>
              <div id="bt-loading" class="hidden text-xs text-gray-400 py-2 text-center">⏳ 回测运行中…</div>
            </div>
          </div>

        </div>

        <!-- ══ 子面板 2：日志 ══ -->
        <div id="sub-log" class="hidden">
          <div class="bg-white border border-gray-100 rounded-2xl shadow-sm overflow-hidden">
            <!-- 顶部状态栏 -->
            <div class="flex items-center justify-between px-4 py-2.5 border-b border-gray-50">
              <div class="flex items-center gap-2">
                <span class="text-xs font-semibold text-gray-700">运行日志</span>
                <span id="run-badge"
                  class="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-500 font-medium">待机</span>
              </div>
              <button id="btn-clear-log"
                class="text-xs text-gray-300 hover:text-gray-500 transition-colors">清空</button>
            </div>
            <!-- 日志框 -->
            <div id="log-box"
              class="min-h-96 max-h-[60vh] overflow-y-auto p-3 bg-gray-950 font-mono"></div>
          </div>
        </div>

        <!-- ══ 子面板 3：报告 ══ -->
        <div id="sub-report" class="hidden space-y-4">

          <!-- 工具栏：复制 + 切换视图 -->
          <div class="flex items-center justify-between">
            <p class="text-sm font-semibold text-gray-800">📄 分析报告</p>
            <div class="flex items-center gap-2">
              <!-- 视图切换 -->
              <div class="flex rounded-lg overflow-hidden border border-gray-200 text-xs">
                <button id="btn-view-structured"
                  class="view-btn px-2.5 py-1 font-medium transition-colors bg-blue-600 text-white">
                  结构化
                </button>
                <button id="btn-view-markdown"
                  class="view-btn px-2.5 py-1 font-medium transition-colors bg-white text-gray-500 hover:bg-gray-50">
                  Markdown
                </button>
              </div>
              <!-- 复制 -->
              <button id="btn-copy-report"
                class="text-xs px-3 py-1.5 bg-gray-100 hover:bg-gray-200 rounded-lg text-gray-600 transition-colors">
                复制
              </button>
            </div>
          </div>

          <!-- 报告内容区 -->
          <div id="report-structured" class="min-h-32"></div>
          <div id="report-markdown"
            class="hidden bg-white border border-gray-100 rounded-2xl p-5 text-sm text-gray-700 leading-relaxed min-h-32"></div>

          <!-- 无报告提示 -->
          <div id="report-empty" class="py-16 text-center text-gray-300 text-sm">
            运行分析后报告将显示在这里
          </div>

          <!-- 历史记录：最近 5 条 -->
          <div id="run-history" class="hidden mt-4">
            <div class="flex items-center gap-2 mb-2">
              <span class="text-xs font-semibold text-gray-500">最近历史</span>
              <button id="btn-clear-history"
                class="text-xs text-gray-300 hover:text-red-400 transition-colors">清空</button>
            </div>
            <div id="run-history-list" class="space-y-1.5"></div>
          </div>

        </div>

      </div>`;

    this._buildDims();
    this._bindEvents();
    this._updateOnlineUI();
    this._report = new ReportView(this._c.querySelector('#report-structured'));
    this._switchSubTab('analyze');
    this._renderHistory();
  }

  // ── 子面板切换 ────────────────────────────────────────────────────────
  _switchSubTab(name) {
    this._activeSub = name;
    ['analyze','log','report'].forEach(p => {
      const el  = this._c.querySelector(`#sub-${p}`);
      const btn = this._c.querySelector(`[data-sub="${p}"]`);
      if (!el || !btn) return;
      const active = p === name;
      el.classList.toggle('hidden', !active);
      if (active) {
        btn.classList.remove('text-gray-500');
        btn.classList.add('bg-white', 'shadow-sm', 'text-gray-900');
      } else {
        btn.classList.remove('bg-white', 'shadow-sm', 'text-gray-900');
        btn.classList.add('text-gray-500');
      }
    });
    if (name === 'log')    this._c.querySelector('#tab-log-badge')?.classList.add('hidden');
    if (name === 'report') this._c.querySelector('#tab-report-badge')?.classList.add('hidden');
  }

  // ── 维度选择器 ────────────────────────────────────────────────────────
  _buildDims() {
    const list = this._c.querySelector('#dim-list');
    Object.entries(DIM_DEFS).forEach(([dimKey, dimDef]) => {
      const c = dimDef.color || 'blue';
      const wrap = document.createElement('div');
      wrap.className = 'border border-gray-100 rounded-xl overflow-hidden';
      const checked = this._selDims.has(dimKey);
      wrap.innerHTML = `
        <div class="flex items-center gap-3 px-3 py-2 bg-gray-50 hover:bg-gray-100 cursor-pointer transition-colors"
             data-dim="${dimKey}">
          <input type="checkbox" id="dim-${dimKey}" ${checked ? 'checked' : ''}
            class="w-4 h-4 accent-${c}-600 cursor-pointer" />
          <label for="dim-${dimKey}" class="text-xs font-medium text-gray-700 cursor-pointer flex-1 select-none">
            ${dimDef.label}
          </label>
          <span class="text-xs text-gray-400">▼</span>
        </div>
        <div class="dim-mods ${checked ? '' : 'hidden'} px-3 py-2 grid grid-cols-2 sm:grid-cols-3 gap-1.5 bg-white"
             data-dim="${dimKey}">
          ${Object.entries(dimDef.mods).map(([mk, ml]) => `
            <label class="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer hover:text-${c}-600">
              <input type="checkbox" class="mod-cb accent-${c}-600 w-3.5 h-3.5"
                data-dim="${dimKey}" data-mod="${mk}"
                ${this._selMods[dimKey]?.has(mk) ? 'checked' : ''} />
              ${ml}
            </label>`).join('')}
        </div>`;
      list.appendChild(wrap);
    });

    list.querySelectorAll('[data-dim]').forEach(row => {
      if (row.classList.contains('dim-mods')) return;
      row.addEventListener('click', e => {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'LABEL') return;
        const cb = row.querySelector('input[type=checkbox]');
        cb.checked = !cb.checked; cb.dispatchEvent(new Event('change'));
      });
    });
    list.querySelectorAll('[id^="dim-"]').forEach(cb => {
      cb.addEventListener('change', () => {
        const k = cb.id.replace('dim-', '');
        const modsEl = list.querySelector(`.dim-mods[data-dim="${k}"]`);
        cb.checked ? this._selDims.add(k) : this._selDims.delete(k);
        modsEl?.classList.toggle('hidden', !cb.checked);
        this._updateDimsCount();
      });
    });
    list.querySelectorAll('.mod-cb').forEach(cb => {
      cb.addEventListener('change', () => {
        const { dim, mod } = cb.dataset;
        cb.checked ? this._selMods[dim]?.add(mod) : this._selMods[dim]?.delete(mod);
      });
    });
  }

  _updateDimsCount() {
    const el = this._c.querySelector('#dims-count');
    if (el) el.textContent = this._selDims.size;
  }

  // ── 自选股芯片 ────────────────────────────────────────────────────────
  _syncChips(state) {
    const chips = this._c.querySelector('#run-chips');
    if (!chips) return;
    const stocks = state.stock_list || [];
    if (!stocks.length) {
      chips.innerHTML = '<span class="text-xs text-gray-300">（自选股为空，先去「自选股」Tab 添加）</span>';
      return;
    }
    chips.innerHTML = stocks.map(s => {
      const label = s.name ? `${this._esc(s.name)}` : s.code;
      return `<button class="chip px-2.5 py-1 text-xs border border-gray-200 rounded-lg text-gray-600"
              data-code="${s.code}" data-name="${this._esc(s.name || '')}">${label}</button>`;
    }).join('');
    chips.querySelectorAll('.chip').forEach(btn => {
      btn.addEventListener('click', () => {
        this._c.querySelector('#run-code').value = btn.dataset.code;
        const nameEl = this._c.querySelector('#run-name');
        if (nameEl) nameEl.value = btn.dataset.name || '';
        chips.querySelectorAll('.chip').forEach(b => b.classList.toggle('selected', b === btn));
      });
    });
  }

  _esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  // ── 在线状态 UI ───────────────────────────────────────────────────────
  _updateOnlineUI() {
    const dot   = this._c.querySelector('#run-dot');
    const label = this._c.querySelector('#run-svc-label');
    const chip  = this._c.querySelector('#run-svc-chip');
    if (!dot) return;
    if (this._online) {
      dot.className     = 'status-dot online';
      label.textContent = '本地服务在线';
      chip.className    = chip.className.replace(/bg-\S+|border-\S+|text-\S+/g, '').trim()
        + ' bg-green-50 border-green-200 text-green-700';
    } else {
      dot.className     = 'status-dot offline';
      label.textContent = '纯静态模式';
      chip.className    = chip.className.replace(/bg-\S+|border-\S+|text-\S+/g, '').trim()
        + ' bg-gray-50 border-gray-200 text-gray-400';
    }
  }

  // ── 事件绑定 ──────────────────────────────────────────────────────────
  _bindEvents() {
    // 子 Tab 切换
    this._c.querySelectorAll('.sub-tab').forEach(btn => {
      btn.addEventListener('click', () => this._switchSubTab(btn.dataset.sub));
    });

    // 分析按钮
    this._c.querySelector('#btn-deep').addEventListener('click',   () => this._deepAnalyze());
    this._c.querySelector('#btn-market').addEventListener('click', () => this._marketReview());
    this._c.querySelector('#btn-full').addEventListener('click',   () => this._quickRun('full'));
    this._c.querySelector('#btn-batch').addEventListener('click',  () => this._batchAnalyze());
    this._c.querySelector('#run-code').addEventListener('keydown', e => {
      if (e.key === 'Enter') this._deepAnalyze();
    });

    // 日志清空
    this._c.querySelector('#btn-clear-log').addEventListener('click', () => {
      this._c.querySelector('#log-box').innerHTML = '';
    });

    // 复制报告
    this._c.querySelector('#btn-copy-report').addEventListener('click', async () => {
      const mbox = this._c.querySelector('#report-markdown');
      const sbox = this._c.querySelector('#report-structured');
      try {
        await navigator.clipboard.writeText(mbox.dataset.raw || sbox.innerText || '');
        this._t.show('已复制', 'success');
      } catch { this._t.show('复制失败', 'warning'); }
    });

    // 视图切换
    this._c.querySelector('#btn-view-structured').addEventListener('click', () => this._setViewMode('structured'));
    this._c.querySelector('#btn-view-markdown').addEventListener('click',   () => this._setViewMode('markdown'));

    // 历史清空
    this._c.querySelector('#btn-clear-history')?.addEventListener('click', () => {
      localStorage.removeItem(HISTORY_KEY);
      this._renderHistory();
    });

    // 回测
    this._c.querySelector('#btn-backtest').addEventListener('click', () => this._runBacktest());
    this._c.querySelector('#bt-code').addEventListener('keydown', e => {
      if (e.key === 'Enter') this._runBacktest();
    });
    // 同步深度分析的股票代码到回测输入框
    this._c.querySelector('#run-code').addEventListener('input', e => {
      const btCode = this._c.querySelector('#bt-code');
      if (!btCode.value) btCode.value = e.target.value;
    });
  }

  // ── 回测 ────────────────────────────────────────────────────────────
  async _runBacktest() {
    const code = (this._c.querySelector('#bt-code').value || '').trim();
    if (!code) { this._t.show('请输入股票代码', 'warning'); return; }

    const loading = this._c.querySelector('#bt-loading');
    const errEl   = this._c.querySelector('#bt-error');
    const resEl   = this._c.querySelector('#bt-result');
    const noteEl  = this._c.querySelector('#bt-note');
    const btn     = this._c.querySelector('#btn-backtest');

    loading.classList.remove('hidden');
    errEl.classList.add('hidden');
    resEl.classList.add('hidden');
    btn.disabled = true;

    try {
      const res = await fetch(`${SERVER}/backtest?code=${encodeURIComponent(code)}`);
      const data = await res.json();
      loading.classList.add('hidden');
      btn.disabled = false;

      if (!res.ok || data.ok === false || data.error) {
        errEl.textContent = '❌ ' + (data.error || `请求失败（HTTP ${res.status}）`);
        errEl.classList.remove('hidden');
        noteEl.textContent = '请先对该股票运行一次深度分析';
        return;
      }
      // 后端返回结构为 { ok, code, backtest: { signals, total_days, stock_days } }，
      // 回测结果字段都在 backtest 层；兼容旧的平铺结构。
      const bt = data.backtest || data;
      noteEl.textContent = `数据区间：${bt.stock_days}  共 ${bt.total_days} 个交易日`;
      this._renderBacktestResult(bt);
    } catch (e) {
      loading.classList.add('hidden');
      btn.disabled = false;
      errEl.textContent = '❌ 请求失败：' + e.message + '（请确认本地服务已启动）';
      errEl.classList.remove('hidden');
    }
  }

  _renderBacktestResult(data) {
    const resEl   = this._c.querySelector('#bt-result');
    const metaEl  = this._c.querySelector('#bt-meta');
    const chartEl = this._c.querySelector('#bt-chart');
    const tableEl = this._c.querySelector('#bt-table');

    resEl.classList.remove('hidden');
    metaEl.textContent = `数据区间：${data.stock_days}  共 ${data.total_days} 个交易日`;

    const signals = data.signals || {};
    const names   = Object.keys(signals).filter(k => signals[k].count > 0);
    const days    = ['1', '3', '5', '10', '20'];
    const colors  = { '1': '#a855f7', '3': '#ec4899', '5': '#3b82f6', '10': '#f59e0b', '20': '#10b981' };

    // ── ECharts 柱状图：各信号 × 三个持有期的胜率 ──────────────
    if (typeof echarts !== 'undefined' && names.length > 0) {
      const chart = echarts.init(chartEl);
      chart.setOption({
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'shadow' },
          formatter: params => {
            let s = `<b>${params[0].axisValue}</b><br/>`;
            params.forEach(p => { s += `${p.marker}${p.seriesName}日胜率：<b>${p.value}%</b><br/>`; });
            return s;
          }
        },
        legend: {
          data: days.map(d => d + '日胜率'),
          textStyle: { fontSize: 11 },
          bottom: 0,
        },
        grid: { left: 10, right: 10, top: 16, bottom: 36, containLabel: true },
        xAxis: {
          type: 'category',
          data: names,
          axisLabel: { fontSize: 10, rotate: names.length > 5 ? 15 : 0, overflow: 'break', width: 80 },
        },
        yAxis: {
          type: 'value', min: 0, max: 100,
          axisLabel: { formatter: '{value}%', fontSize: 10 },
          splitLine: { lineStyle: { color: '#f0f0f0' } },
        },
        series: days.map(d => ({
          name: d + '日胜率',
          type: 'bar',
          barGap: '5%',
          barCategoryGap: '30%',
          itemStyle: { color: colors[d], borderRadius: [3, 3, 0, 0] },
          data: names.map(n => signals[n].stats?.[d]?.win_rate ?? 0),
          label: { show: true, position: 'top', fontSize: 9,
                   formatter: p => p.value > 0 ? p.value + '%' : '' },
        })),
      });
      window.addEventListener('resize', () => chart.resize());
    } else {
      chartEl.innerHTML = '<div class="text-xs text-gray-400 text-center py-8">无信号触发记录</div>';
    }

    // ── 明细表格 ────────────────────────────────────────────────
    const rows = names.map(n => {
      const s = signals[n];
      const stat = s.stats || {};
      return `<tr class="border-b border-gray-50 hover:bg-gray-50 transition-colors">
        <td class="px-3 py-2 text-xs font-medium text-gray-700 whitespace-nowrap">${n}</td>
        <td class="px-3 py-2 text-xs text-center text-gray-500">${s.count}</td>
        ${days.map(d => {
          const w  = stat[d]?.win_rate  ?? '-';
          const r  = stat[d]?.avg_return ?? '-';
          const wc = typeof w === 'number' ? (w >= 60 ? 'text-emerald-600 font-semibold' : w < 45 ? 'text-red-500' : 'text-gray-600') : 'text-gray-400';
          const rc = typeof r === 'number' ? (r > 0 ? 'text-emerald-600' : r < 0 ? 'text-red-500' : 'text-gray-500') : 'text-gray-400';
          return `<td class="px-2 py-2 text-xs text-center ${wc}">${typeof w === 'number' ? w + '%' : w}</td>
                  <td class="px-2 py-2 text-xs text-center ${rc}">${typeof r === 'number' ? (r > 0 ? '+' : '') + r + '%' : r}</td>`;
        }).join('')}
      </tr>`;
    }).join('');

    const allZero = names.length === 0;
    tableEl.innerHTML = allZero
      ? '<p class="text-xs text-gray-400 text-center py-4">历史数据中未触发任何信号</p>'
      : `<table class="w-full text-left border-collapse">
           <thead>
             <tr class="bg-gray-50 text-xs text-gray-500">
               <th class="px-3 py-2 font-medium">信号名称</th>
               <th class="px-3 py-2 font-medium text-center">触发次数</th>
               ${days.map(d =>
                 `<th class="px-2 py-2 font-medium text-center" colspan="2">${d} 日持有</th>`
               ).join('')}
             </tr>
             <tr class="bg-gray-50 text-xs text-gray-400 border-b border-gray-100">
               <th></th><th></th>
               ${days.map(() => '<th class="px-2 py-1 text-center">胜率</th><th class="px-2 py-1 text-center">均收益</th>').join('')}
             </tr>
           </thead>
           <tbody>${rows}</tbody>
         </table>
         <p class="text-xs text-gray-400 mt-2 px-1">胜率≥60% 绿色 · <45% 红色；均收益正负对应涨跌</p>`;
  }

  // ── 视图切换（结构化 / Markdown）────────────────────────────────────
  _setViewMode(mode) {
    this._viewMode = mode;
    const btnS = this._c.querySelector('#btn-view-structured');
    const btnM = this._c.querySelector('#btn-view-markdown');
    const sbox = this._c.querySelector('#report-structured');
    const mbox = this._c.querySelector('#report-markdown');

    if (mode === 'structured') {
      btnS.className = btnS.className.replace('bg-white text-gray-500 hover:bg-gray-50','') + ' bg-blue-600 text-white';
      btnM.className = btnM.className.replace('bg-blue-600 text-white','') + ' bg-white text-gray-500 hover:bg-gray-50';
      sbox.classList.remove('hidden');
      mbox.classList.add('hidden');
    } else {
      btnM.className = btnM.className.replace('bg-white text-gray-500 hover:bg-gray-50','') + ' bg-blue-600 text-white';
      btnS.className = btnS.className.replace('bg-blue-600 text-white','') + ' bg-white text-gray-500 hover:bg-gray-50';
      mbox.classList.remove('hidden');
      sbox.classList.add('hidden');
      if (!mbox.dataset.raw && this._lastRawReport && this._lastRtype === 'structured') {
        mbox.dataset.raw = typeof this._lastRawReport === 'string'
          ? this._lastRawReport
          : JSON.stringify(this._lastRawReport, null, 2);
        mbox.innerHTML = this._md(mbox.dataset.raw);
      }
    }
  }

  // ── 自动启动 server ───────────────────────────────────────────────────
  async _ensureServer() {
    if (this._online) return true;
    if (this._serverStarting) {
      this._t.show('正在等待本地服务启动...', 'warning');
      return false;
    }
    this._serverStarting = true;
    this._t.show('需要本地服务，请运行：python portal/server.py', 'warning', 8000);

    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 1000));
      try {
        const r = await fetch(`${SERVER}/health`, { signal: AbortSignal.timeout(1000) });
        if (r.ok) {
          this._online = true;
          this._serverStarting = false;
          this._updateOnlineUI();
          window.dispatchEvent(new CustomEvent('server-online'));
          return true;
        }
      } catch {}
    }
    this._serverStarting = false;
    this._t.show('本地服务未启动，请先运行 server.py', 'error');
    return false;
  }

  // ── 深度分析（单股，读输入框）──────────────────────────────────────────
  async _deepAnalyze() {
    const code = this._c.querySelector('#run-code').value.trim().toUpperCase();
    const name = this._c.querySelector('#run-name').value.trim();
    if (!code) { this._t.show('请输入股票代码', 'warning'); return; }

    const dims = [...this._selDims];
    if (!dims.length) { this._t.show('请至少选择一个分析维度', 'warning'); return; }

    const ok = await this._ensureServer();
    if (!ok) return;

    await this._runSingle(code, name || code, dims);
  }

  // ── 批量分析勾选的自选股（串行队列）──────────────────────────────────
  async _batchAnalyze() {
    const dims = [...this._selDims];
    if (!dims.length) { this._t.show('请至少选择一个分析维度', 'warning'); return; }

    const state = this._s.get();
    const checked = (state.stock_list || []).filter(s => s.checked);
    if (!checked.length) {
      this._t.show('请先在「自选股」Tab 勾选要分析的股票', 'warning');
      return;
    }

    const ok = await this._ensureServer();
    if (!ok) return;

    this._prepareLog(`📑 批量分析 ${checked.length} 只自选股`);
    this._disableBtns();

    let done = 0, failed = 0;
    for (const s of checked) {
      done++;
      this._disableBtns();   // 每只前重新禁用（_runSingle 内 SSE done 会解禁）
      this._appendLog(`──────── (${done}/${checked.length}) ${s.name || s.code} ────────`, 'info');
      try {
        const okOne = await this._runSingle(s.code, s.name || s.code, dims, { batch: true });
        if (!okOne) failed++;
      } catch (e) {
        failed++;
        this._appendLog(`❌ ${s.code} 分析异常：${e.message}`, 'error');
      }
    }

    const badge = this._c.querySelector('#run-badge');
    badge.textContent = `批量完成 ${done - failed}/${done}`;
    badge.className = `text-xs px-2 py-0.5 rounded-full font-medium ${failed ? 'bg-amber-100 text-amber-700' : 'bg-green-100 text-green-700'}`;
    this._resetBtns();
    this._t.show(`批量分析完成：成功 ${done - failed} / 共 ${done}`, failed ? 'warning' : 'success', 6000);
    // 批量结束切到报告页看最近历史
    this._switchSubTab('report');
  }

  /**
   * 分析单只股票 → Promise<boolean>（成功 true / 失败 false，永不 reject）。
   * 供单股按钮和批量队列共用。batch=true 时不重置日志（保留队列上下文）。
   */
  async _runSingle(code, name, dims, { batch = false } = {}) {
    const modulesMap = Object.fromEntries(dims.map(d => [d, [...(this._selMods[d] || [])]]));
    if (!batch) {
      this._prepareLog(`🔬 深度分析：${name || code}（${code}）`);
    } else {
      this._appendLog(`🔬 深度分析：${name || code}（${code}）`, 'info');
    }

    let taskId;
    try {
      const res = await fetch(`${SERVER}/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stock_code: code, stock_name: name || code, dimensions: dims, modules: modulesMap,
          llm_mode: this._s.get().llm_note_mode || 'batch',
          open_report: this._s.get().auto_open_html !== false,
          agent_review: !!this._c.querySelector('#chk-agent-review')?.checked }),
      });
      const d = await res.json();
      if (!d.ok) throw new Error(d.error);
      taskId = d.task_id;
    } catch (e) {
      this._appendLog(`❌ 启动失败：${e.message}`, 'error');
      if (!batch) { this._t.show(`启动失败: ${e.message}`, 'error'); this._resetBtns(); }
      return false;
    }
    return this._listenSSE(taskId, 'structured', code, name || code);
  }

  // ── 快速任务 ──────────────────────────────────────────────────────────
  async _quickRun(mode) {
    const ok = await this._ensureServer();
    if (!ok) return;

    const label = mode === 'market' ? '🌐 大盘复盘' : '⚡ 全量分析';
    this._prepareLog(label);

    let taskId;
    try {
      const res = await fetch(`${SERVER}/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, force_run: false, no_notify: true }),
      });
      const d = await res.json();
      if (!d.ok) throw new Error(d.error);
      taskId = d.task_id;
    } catch (e) {
      this._t.show(`启动失败: ${e.message}`, 'error');
      this._resetBtns(); return;
    }
    this._listenSSE(taskId, 'markdown', '', label);
  }

  // ── 大盘复盘（新链路：上证+创业板，生成HTML）──────────────────────────
  async _marketReview() {
    const ok = await this._ensureServer();
    if (!ok) return;
    this._prepareLog('🌐 大盘复盘（上证指数 + 创业板指）');
    let taskId;
    try {
      const res = await fetch(`${SERVER}/market_review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ open_report: this._s.get().auto_open_html !== false }),
      });
      const d = await res.json();
      if (!d.ok) throw new Error(d.error);
      taskId = d.task_id;
    } catch (e) {
      this._t.show(`启动失败: ${e.message}`, 'error');
      this._resetBtns(); return;
    }
    this._listenSSE(taskId, 'market', '__market__', '大盘复盘');
  }

  // ── 日志区准备（切换到日志子面板）──────────────────────────────────
  _prepareLog(title) {
    if (this._sse) { this._sse.close(); this._sse = null; }
    this._c.querySelector('#log-box').innerHTML = '';
    this._c.querySelector('#report-structured').innerHTML = '';
    const badge = this._c.querySelector('#run-badge');
    badge.textContent = '运行中';
    badge.className = 'text-xs px-2 py-0.5 rounded-full bg-blue-100 text-blue-700 font-medium';
    this._appendLog(title, 'info');
    this._disableBtns();
    // 自动切换到日志子面板
    this._switchSubTab('log');
  }

  // ── SSE 监听（返回 Promise，done/error 都 resolve，供批量队列 await）──
  _listenSSE(taskId, rtype, code = '', subject = '') {
    return new Promise((resolve) => {
      const sse = new EventSource(`${SERVER}/run/stream/${taskId}`);
      this._sse = sse;
      let settled = false;
      const finish = (ok) => { if (!settled) { settled = true; resolve(ok); } };

      sse.onmessage = e => {
        const d = JSON.parse(e.data);
        this._appendLog(d.log, d.status === 'error' ? 'error' : 'default');
      };
      sse.addEventListener('done', e => {
        sse.close(); this._sse = null;
        const d = JSON.parse(e.data);
        const ok = d.status === 'done';
        const badge = this._c.querySelector('#run-badge');
        badge.textContent = ok ? '完成 ✓' : '失败 ✗';
        badge.className = `text-xs px-2 py-0.5 rounded-full font-medium ${ok ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`;
        this._resetBtns();
        if (d.has_report) {
          this._loadReport(taskId, d.report_type || rtype, code, subject).finally(() => finish(ok));
        } else {
          finish(ok);
        }
      });
      sse.onerror = () => {
        sse.close(); this._sse = null;
        this._appendLog('⚠️ 连接断开，拉取结果...', 'warn');
        setTimeout(() => {
          this._loadReport(taskId, rtype, code, subject).finally(() => finish(false));
        }, 600);
        this._resetBtns();
      };
    });
  }

  async _loadReport(taskId, rtype, code = '', subject = '') {
    try {
      const res = await fetch(`${SERVER}/run/report/${taskId}`);
      const d = await res.json();
      const box = this._c.querySelector('#log-box');
      if (d.logs) {
        const ex = box.children.length;
        d.logs.slice(ex).forEach(l => this._appendLog(l));
      }
      if (d.report) {
        this._lastRtype = rtype;
        if (rtype === 'market') {
          // 大盘：HTML 已在浏览器打开，portal 内不渲染，只提示 + 记历史
          this._t.show('大盘复盘完成，HTML 已在浏览器打开', 'success', 5000);
          this._saveHistory({ code: '__market__', name: '大盘复盘', score: null, signal: '', taskId }, null);
        } else {
          rtype === 'structured'
            ? this._showStructured(d.report, code, subject, taskId)
            : this._showMarkdown(d.report, code, subject, taskId);
        }
      }
    } catch {}
  }

  _showStructured(json, code = '', subject = '', taskId = '') {
    try {
      const rpt = typeof json === 'string' ? JSON.parse(json) : json;
      this._lastRawReport = rpt;
      const sbox  = this._c.querySelector('#report-structured');
      const mbox  = this._c.querySelector('#report-markdown');
      const empty = this._c.querySelector('#report-empty');
      sbox.classList.remove('hidden');
      mbox.classList.add('hidden');
      mbox.dataset.raw = '';
      if (empty) empty.classList.add('hidden');
      this._report.render(rpt);
      // 🤖 Agent 综合研判（选项B）：若有则在结构化报告末尾追加一节
      if (rpt && rpt.agent_review) {
        const card = document.createElement('div');
        card.className = 'mt-4 p-3 rounded-xl border border-blue-200 bg-blue-50/60';
        card.innerHTML =
          `<div class="text-xs font-bold text-blue-700 mb-1.5">🤖 Agent 综合研判</div>` +
          `<div class="text-xs leading-relaxed text-gray-800">${this._md(String(rpt.agent_review))}</div>`;
        sbox.appendChild(card);
      }
      this._setViewMode('structured');
      // 从报告中提取评分和信号
      const score  = rpt?.summary?.score  ?? rpt?.score  ?? null;
      const signal = rpt?.summary?.signal ?? rpt?.signal ?? '';
      this._saveHistory({ code, name: subject, score, signal, taskId }, rpt);
      // 红点 + 切到报告子面板
      this._c.querySelector('#tab-report-badge')?.classList.remove('hidden');
      this._switchSubTab('report');
    } catch { this._showMarkdown(String(json), code, subject, taskId); }
  }

  _showMarkdown(md, code = '', subject = '', taskId = '') {
    const sbox  = this._c.querySelector('#report-structured');
    const mbox  = this._c.querySelector('#report-markdown');
    const empty = this._c.querySelector('#report-empty');
    this._lastRawReport = md;
    sbox.innerHTML = '';
    sbox.classList.add('hidden');
    mbox.dataset.raw = md;
    mbox.innerHTML = this._md(md);
    mbox.classList.remove('hidden');
    if (empty) empty.classList.add('hidden');
    this._setViewMode('markdown');
    this._saveHistory({ code, name: subject, score: null, signal: '', taskId }, md);
    // 红点 + 切到报告子面板
    this._c.querySelector('#tab-report-badge')?.classList.remove('hidden');
    this._switchSubTab('report');
  }

  // ── 日志追加（带 [HH:MM:SS] 时间戳）────────────────────────────────
  _appendLog(line, type = 'default') {
    const box = this._c.querySelector('#log-box');
    if (!line || !box) return;

    const now = new Date();
    const ts  = [now.getHours(), now.getMinutes(), now.getSeconds()]
      .map(n => String(n).padStart(2, '0')).join(':');

    const row = document.createElement('div');
    row.className = 'flex gap-2 text-xs leading-relaxed font-mono py-0.5';

    const colors = {
      info:    '#6ee7b7',
      error:   '#fca5a5',
      warn:    '#fcd34d',
      default: '#d1fae5',
    };
    let col = colors.default;
    if (/✅|成功|完成/.test(line))      col = colors.info;
    else if (/❌|失败|ERROR/.test(line)) col = colors.error;
    else if (/⚠|WARNING/.test(line))    col = colors.warn;

    const tsEl = document.createElement('span');
    tsEl.className   = 'shrink-0 select-none';
    tsEl.style.color = '#4b5563';
    tsEl.textContent = `[${ts}]`;

    const msgEl = document.createElement('span');
    msgEl.style.color = col;
    msgEl.textContent  = line;

    row.appendChild(tsEl);
    row.appendChild(msgEl);
    box.appendChild(row);
    box.scrollTop = box.scrollHeight;

    if (this._activeSub !== 'log') {
      this._c.querySelector('#tab-log-badge')?.classList.remove('hidden');
    }
  }

  // ── 历史记录（localStorage，最多 10 条，显示最近 5 条）──────────────
  // 格式：[{code, name, score, signal, time, taskId}]
  _saveHistory({ code, name, score, signal, taskId } = {}, rpt = null) {
    let list = this._loadHistoryList();
    // 去重（需求1）：同 code 只保留最新——删旧项及其报告缓存
    if (code) {
      list.filter(h => h.code === code).forEach(h => {
        if (h.taskId) { try { localStorage.removeItem(REPORT_CACHE_PREFIX + h.taskId); } catch {} }
      });
      list = list.filter(h => h.code !== code);
    }
    list.unshift({
      code:   code   || '',
      name:   name   || code || '',
      score:  score  ?? null,
      signal: signal || '',
      time:   Date.now(),
      taskId: taskId || '',
    });
    if (list.length > HISTORY_MAX) list.length = HISTORY_MAX;
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(list)); } catch {}
    // 缓存完整报告 JSON（< 200KB 才写入）
    if (rpt != null && taskId) {
      try {
        const reportStr = JSON.stringify(rpt);
        if (reportStr.length < 200 * 1024) {
          try { localStorage.setItem(REPORT_CACHE_PREFIX + taskId, reportStr); } catch(e) {}
        }
      } catch(e) {}
    }
    this._renderHistory();
  }

  _loadHistoryList() {
    try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); }
    catch { return []; }
  }

  _cleanOldReportCache() {
    try {
      const list = this._loadHistoryList();
      const validIds = new Set(list.map(h => h.taskId).filter(Boolean));
      const keysToDelete = [];
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key && key.startsWith(REPORT_CACHE_PREFIX)) {
          const taskId = key.slice(REPORT_CACHE_PREFIX.length);
          if (!validIds.has(taskId)) keysToDelete.push(key);
        }
      }
      keysToDelete.forEach(k => localStorage.removeItem(k));
    } catch(e) {}
  }

  _renderHistory() {
    const sec  = this._c.querySelector('#run-history');
    const list = this._c.querySelector('#run-history-list');
    if (!sec || !list) return;
    const items = this._loadHistoryList().slice(0, HISTORY_SHOW);
    if (!items.length) { sec.classList.add('hidden'); return; }
    sec.classList.remove('hidden');

    // 清理孤立缓存（历史列表中已不存在的 taskId）
    this._cleanOldReportCache();

    list.innerHTML = items.map((h, i) => {
      const d    = new Date(h.time);
      const time = `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
      const scoreBadge = h.score != null
        ? `<span class="px-1.5 py-0.5 bg-blue-50 text-blue-600 rounded text-xs font-mono">${h.score}</span>`
        : '';
      const signalBadge = h.signal
        ? `<span class="px-1.5 py-0.5 bg-gray-100 text-gray-500 rounded text-xs">${h.signal}</span>`
        : '';
      const label = h.code
        ? `${h.code}${h.name && h.name !== h.code ? ' · ' + h.name : ''}`
        : (h.name || '分析报告');
      const cached = h.taskId && localStorage.getItem(REPORT_CACHE_PREFIX + h.taskId) != null;
      const cacheIcon = cached
        ? `<span title="已缓存，可离线查看" class="shrink-0 text-xs">💾</span>`
        : `<span title="需联网拉取" class="shrink-0 text-xs">🌐</span>`;
      return `
        <div class="flex items-center gap-2 px-3 py-2 bg-gray-50 rounded-xl text-xs text-gray-600
                    hover:bg-gray-100 cursor-pointer transition-colors"
             data-history-idx="${i}">
          ${cacheIcon}
          ${scoreBadge}
          ${signalBadge}
          <span class="flex-1 font-medium truncate">${label}</span>
          <span class="text-gray-400 shrink-0">${time}</span>
        </div>`;
    }).join('');

    // 点击历史条目：优先读缓存，否则走网络
    list.querySelectorAll('[data-history-idx]').forEach(row => {
      row.addEventListener('click', async () => {
        const idx  = parseInt(row.dataset.historyIdx, 10);
        const item = this._loadHistoryList()[idx];
        if (!item) return;
        // 优先：在线时让 server 打开该 code 最新 HTML 报告（需求2）
        if (item.code) {
          try {
            const r = await fetch(`${SERVER}/open_report?code=${encodeURIComponent(item.code)}`,
                                  { signal: AbortSignal.timeout(3000) });
            const d = await r.json();
            if (d.ok) { this._t.show('已在浏览器打开报告', 'success'); return; }
          } catch {}
        }
        // 回退：缓存结构化视图（离线兜底）
        if (!item.taskId) { this._t.show('无缓存报告，请重新分析', 'warning'); return; }
        const cached = localStorage.getItem(REPORT_CACHE_PREFIX + item.taskId);
        if (cached) {
          try {
            const rpt = JSON.parse(cached);
            if (typeof rpt === 'object' && rpt !== null) {
              this._lastRtype = 'structured';
              this._showStructuredFromCache(rpt, item.code, item.name);
            } else {
              this._lastRtype = 'markdown';
              this._showMarkdownFromCache(String(rpt), item.code, item.name);
            }
            return;
          } catch(e) {}
        }
        this._loadReport(item.taskId, 'structured', item.code, item.name);
      });
    });
  }

  // 从缓存渲染结构化报告（不重新写历史、不重新缓存）
  _showStructuredFromCache(rpt, code = '', subject = '') {
    try {
      this._lastRawReport = rpt;
      const sbox  = this._c.querySelector('#report-structured');
      const mbox  = this._c.querySelector('#report-markdown');
      const empty = this._c.querySelector('#report-empty');
      sbox.classList.remove('hidden');
      mbox.classList.add('hidden');
      mbox.dataset.raw = '';
      if (empty) empty.classList.add('hidden');
      this._report.render(rpt);
      this._setViewMode('structured');
      this._t.show('(已缓存)', 'success');
      this._c.querySelector('#tab-report-badge')?.classList.remove('hidden');
      this._switchSubTab('report');
    } catch(e) { this._t.show('缓存解析失败', 'warning'); }
  }

  // 从缓存渲染 markdown 报告
  _showMarkdownFromCache(md, code = '', subject = '') {
    const sbox  = this._c.querySelector('#report-structured');
    const mbox  = this._c.querySelector('#report-markdown');
    const empty = this._c.querySelector('#report-empty');
    this._lastRawReport = md;
    sbox.innerHTML = '';
    sbox.classList.add('hidden');
    mbox.dataset.raw = md;
    mbox.innerHTML = this._md(md);
    mbox.classList.remove('hidden');
    if (empty) empty.classList.add('hidden');
    this._setViewMode('markdown');
    this._t.show('(已缓存)', 'success');
    this._c.querySelector('#tab-report-badge')?.classList.remove('hidden');
    this._switchSubTab('report');
  }

  // ── 按钮状态 ──────────────────────────────────────────────────────────
  _disableBtns() {
    ['#btn-deep','#btn-market','#btn-full','#btn-batch'].forEach(s => {
      const el = this._c.querySelector(s);
      if (el) el.disabled = true;
    });
  }
  _resetBtns() {
    ['#btn-deep','#btn-market','#btn-full','#btn-batch'].forEach(s => {
      const el = this._c.querySelector(s);
      if (el) el.disabled = false;
    });
  }

  // ── 简易 Markdown -> HTML ────────────────────────────────────────────
  _md(md) {
    const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return md.split('\n').map(l => {
      if (/^### (.+)/.test(l)) return `<h3 class="font-bold mt-3 mb-1 text-sm">${esc(l.slice(4))}</h3>`;
      if (/^## (.+)/.test(l))  return `<h2 class="font-bold mt-4 mb-2 text-base border-b pb-1">${esc(l.slice(3))}</h2>`;
      if (/^# (.+)/.test(l))   return `<h1 class="font-bold mt-5 mb-2 text-lg">${esc(l.slice(2))}</h1>`;
      if (/^---+$/.test(l))    return '<hr class="my-3 border-gray-200"/>';
      if (/^[-*] (.+)/.test(l)) return `<p class="ml-4">• ${esc(l.slice(2))}</p>`;
      if (!l.trim()) return '<br/>';
      let h = esc(l);
      h = h.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
      h = h.replace(/`(.+?)`/g,'<code class="bg-gray-100 px-1 rounded text-blue-700 text-xs">$1</code>');
      return `<p>${h}</p>`;
    }).join('\n');
  }
}
