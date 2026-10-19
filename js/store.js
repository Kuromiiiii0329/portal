/**
 * store.js — 全局状态管理 + localStorage 持久化
 *
 * stock_list 结构（v2.1 起）：对象数组 [{code, name, checked}]
 *   - code:    股票代码（大写），唯一键
 *   - name:    股票名称（添加时自动查 /quote 填充，可空回退显示代码）
 *   - checked: 是否勾选参与"分析勾选自选股"批量任务（默认 true）
 * 向后兼容：旧数据是字符串数组 ["600519"]，_load 时自动迁移。
 *
 * ⚠️ toJSON() 导出到 config/watchlist.json 时把 stock_list 降维回
 *    纯代码字符串数组 ["600519",...]，因为 GitHub Action
 *    (portal-daily-analysis.yml) 读取它期望字符串数组。契约不能破。
 */

const KEY = 'dsa_portal_v2';

const DEFAULTS = {
  stock_list: [],   // [{code, name, checked}]
  report_type: 'simple',
  market_review_enabled: true,
  market_review_region: 'cn',
  analysis_delay: 0,
  max_workers: 1,
  force_run: false,
  llm_note_mode: 'batch',    // 逐指标LLM点评：batch(批量一次) | per_indicator(逐个详细)
  auto_open_html: true,      // 分析后自动用浏览器打开 HTML 报告
  email: { enabled: true, subject_prefix: 'A股智能分析' },
};

class Store {
  constructor() {
    this._state = this._load();
    this._subs = [];
  }

  get() { return structuredClone(this._state); }
  /** 别名，兼容历史调用（watchlist.js 曾误用 getState） */
  getState() { return this.get(); }

  // ── 自选股操作（对象数组）───────────────────────────────
  addStock(raw, name = '') {
    const code = String(raw || '').trim().toUpperCase();
    if (!code) return { ok: false, msg: '代码不能为空' };
    if (this._state.stock_list.some(s => s.code === code))
      return { ok: false, msg: `${code} 已存在` };
    this._state.stock_list = [...this._state.stock_list, { code, name: name || '', checked: true }];
    this._save(); this._emit(); return { ok: true };
  }

  removeStock(code) {
    this._state.stock_list = this._state.stock_list.filter(s => s.code !== code);
    this._save(); this._emit();
  }

  setStockName(code, name) {
    let changed = false;
    this._state.stock_list = this._state.stock_list.map(s => {
      if (s.code === code && name && s.name !== name) { changed = true; return { ...s, name }; }
      return s;
    });
    if (changed) { this._save(); this._emit(); }
  }

  toggleChecked(code, value) {
    this._state.stock_list = this._state.stock_list.map(s =>
      s.code === code ? { ...s, checked: (value === undefined ? !s.checked : !!value) } : s);
    this._save(); this._emit();
  }

  setAllChecked(value) {
    this._state.stock_list = this._state.stock_list.map(s => ({ ...s, checked: !!value }));
    this._save(); this._emit();
  }

  /** 返回勾选的股票 [{code, name, checked}] */
  getChecked() {
    return this._state.stock_list.filter(s => s.checked);
  }

  set(key, value) {
    if (key.includes('.')) {
      const [p, c] = key.split('.');
      this._state[p] = { ...this._state[p], [c]: value };
    } else {
      this._state[key] = value;
    }
    this._save(); this._emit();
  }

  subscribe(fn) { this._subs.push(fn); fn(this.get()); }

  /**
   * 导出到 config/watchlist.json。
   * ⚠️ stock_list 降维为纯代码字符串数组（GitHub Action 契约）。
   */
  toJSON() {
    const { stock_list, ...rest } = this._state;
    return JSON.stringify({
      _schema_version: '1',
      _comment: '由 portal/index.html 生成，提交到仓库后被 portal-daily-analysis.yml 读取',
      updated_at: new Date().toISOString(),
      ...rest,
      stock_list: stock_list.map(s => s.code),   // 降维：只导出代码
    }, null, 2);
  }

  _emit() { const s = this.get(); this._subs.forEach(f => f(s)); }

  _save() {
    try { localStorage.setItem(KEY, JSON.stringify(this._state)); } catch {}
  }

  _load() {
    try {
      const raw = localStorage.getItem(KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        parsed.stock_list = this._migrateStockList(parsed.stock_list);
        return this._merge(DEFAULTS, parsed);
      }
    } catch {}
    return structuredClone(DEFAULTS);
  }

  /** 把旧的字符串数组 ["600519"] 迁移为对象数组 [{code,name,checked}] */
  _migrateStockList(list) {
    if (!Array.isArray(list)) return [];
    return list.map(item => {
      if (typeof item === 'string') return { code: item.toUpperCase(), name: '', checked: true };
      if (item && typeof item === 'object' && item.code)
        return { code: String(item.code).toUpperCase(), name: item.name || '', checked: item.checked !== false };
      return null;
    }).filter(Boolean);
  }

  _merge(base, over) {
    const r = structuredClone(base);
    for (const k of Object.keys(over)) {
      if (k in r && typeof r[k] === 'object' && !Array.isArray(r[k]) && r[k] !== null)
        r[k] = this._merge(r[k], over[k]);
      else r[k] = over[k];
    }
    return r;
  }
}

export const store = new Store();
