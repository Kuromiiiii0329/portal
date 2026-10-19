/**
 * tabs/settings.js — 分析参数设置（纯静态，无需 server）
 * Toggle 用 JS 驱动，不依赖 Tailwind peer（innerHTML 里 peer 无效）
 */
import { SERVER } from '../config.js';

export class SettingsTab {
  constructor(container, store, toast) {
    this._c = container; this._s = store; this._t = toast;
  }

  init() {
    this._c.innerHTML = `
      <div class="max-w-2xl mx-auto space-y-6 pb-20">
        <div>
          <h2 class="text-base font-semibold text-gray-900">分析设置</h2>
          <p class="text-xs text-gray-500 mt-0.5">修改后点击底部「保存配置」导出</p>
        </div>

        <div class="bg-white rounded-2xl border border-gray-100 shadow-sm divide-y divide-gray-50">

          ${this._row('报告类型', `
            <select id="cfg-report-type" class="form-select w-48">
              <option value="simple">简洁（推荐）</option>
              <option value="brief">极简</option>
              <option value="full">完整</option>
            </select>`, '影响邮件内容详细程度')}

          ${this._row('技术指标 LLM 打分', `
            <select id="cfg-llm-mode" class="form-select w-48">
              <option value="batch">批量打分（打包一次，快，推荐）</option>
              <option value="per_indicator">逐指标打分（单独调用，慢，更精细）</option>
            </select>`, '技术面每个指标由 LLM 基于真实数值打分(0-100)并给出信号与说明')}

          ${this._row('分析后自动打开报告', `
            <div class="flex items-center gap-3">
              <button id="cfg-openhtml-btn" type="button"
                class="relative flex-shrink-0 w-10 h-5 rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-400 focus:ring-offset-1 bg-blue-500"
                role="switch" aria-checked="true">
                <span id="cfg-openhtml-thumb"
                  class="absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform translate-x-5"></span>
              </button>
              <input type="checkbox" id="cfg-openhtml" class="hidden" checked />
              <span class="text-sm text-gray-700" id="cfg-openhtml-label">已启用</span>
            </div>`, '生成 HTML 后用浏览器自动打开')}

          ${this._row('大盘复盘', `
            <div class="flex items-center gap-3">
              <button id="cfg-market-review-btn" type="button"
                class="relative flex-shrink-0 w-10 h-5 rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-400 focus:ring-offset-1 bg-blue-500"
                role="switch" aria-checked="true">
                <span id="cfg-market-thumb"
                  class="absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform translate-x-5"></span>
              </button>
              <input type="checkbox" id="cfg-market-review" class="hidden" checked />
              <span class="text-sm text-gray-700" id="cfg-market-label">已启用</span>
            </div>`, '每日附带大盘指数复盘')}

          ${this._row('大盘市场', `
            <select id="cfg-market-region" class="form-select w-40">
              <option value="cn">A股（沪深）</option>
              <option value="hk">港股</option>
              <option value="us">美股</option>
              <option value="both">全市场</option>
            </select>`)}

          ${this._row('分析并发数', `
            <div class="flex items-center gap-2">
              <input type="number" id="cfg-workers" min="1" max="5" class="form-input w-20" />
              <span class="text-xs text-gray-400">建议 1-3，过高易限流</span>
            </div>`)}

          ${this._row('LLM 间隔(秒)', `
            <div class="flex items-center gap-2">
              <input type="number" id="cfg-delay" min="0" max="300" class="form-input w-20" />
              <span class="text-xs text-gray-400">防止 API 限流</span>
            </div>`)}

          ${this._row('强制运行', `
            <div class="flex items-center gap-3">
              <button id="cfg-force-btn" type="button"
                class="relative flex-shrink-0 w-10 h-5 rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-400 focus:ring-offset-1 bg-gray-200"
                role="switch" aria-checked="false">
                <span id="cfg-force-thumb"
                  class="absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform translate-x-0"></span>
              </button>
              <input type="checkbox" id="cfg-force" class="hidden" />
              <span class="text-xs text-gray-500">跳过交易日检查（含节假日）</span>
            </div>`)}

          ${this._row('邮件主题前缀', `
            <input type="text" id="cfg-email-prefix" maxlength="30" placeholder="A股智能分析"
              class="form-input w-56" />`)}
        </div>

        <!-- AI Key 说明 -->
        <div class="bg-amber-50 border border-amber-100 rounded-2xl p-4">
          <p class="text-xs font-semibold text-amber-800 mb-1.5">🔑 AI 密钥 &amp; 邮件配置</p>
          <p class="text-xs text-amber-700 leading-relaxed">
            <code class="bg-amber-100 px-1 rounded">GEMINI_API_KEY</code>、
            <code class="bg-amber-100 px-1 rounded">EMAIL_SENDER</code>、
            <code class="bg-amber-100 px-1 rounded">EMAIL_PASSWORD</code> 等敏感配置
            存储在 GitHub 仓库 <strong>Settings → Secrets</strong> 中，不在此页面填写。
            详见「说明」Tab。
          </p>
        </div>

        <!-- API 配置 -->
        <div class="bg-white border border-gray-100 rounded-2xl shadow-sm overflow-hidden mt-4">
          <details>
            <summary class="px-5 py-3.5 cursor-pointer font-semibold text-gray-800 text-sm">🔐 本地 API 配置（.env）</summary>
            <div class="px-5 pb-4 border-t border-gray-50">

              <!-- Hai Proxy 子区块（SAP 内网 LLM 网关，优先级最高）-->
              <p class="text-xs font-semibold text-gray-500 uppercase tracking-wide mt-4 mb-1">Hai Proxy（内网 LLM 网关，推荐）</p>
              <p class="text-xs text-gray-400 mb-3">配置后优先使用；适合外网 LLM 被内网封锁的场景</p>
              <div class="space-y-4">
                ${this._envField('HAI_BASE_URL', 'Proxy Base URL', 'text', '如 http://localhost:6655/openai/v1')}
                ${this._envField('HAI_API_KEY',  'Proxy API Key',  'password', '')}
                ${this._envField('HAI_MODEL',    '模型名',          'text', '如 gpt-4.1')}
              </div>

              <!-- LLM 子区块 -->
              <p class="text-xs font-semibold text-gray-500 uppercase tracking-wide mt-5 mb-3">LLM（公网直连，备选）</p>
              <div class="space-y-4">
                ${this._envField('GEMINI_API_KEY',  'Gemini API Key',   'password', '在 aistudio.google.com 获取')}
                ${this._envField('DEEPSEEK_API_KEY','DeepSeek API Key', 'password', '在 platform.deepseek.com 获取')}
                ${this._envField('OPENAI_API_KEY',  'OpenAI API Key',   'password', '在 platform.openai.com 获取')}
              </div>

              <!-- 搜索 子区块 -->
              <p class="text-xs font-semibold text-gray-500 uppercase tracking-wide mt-5 mb-3">Search</p>
              <div class="space-y-4">
                ${this._envField('BOCHA_API_KEYS',  'Bocha 搜索 Key',  'password', '在 bochaai.com 获取，可选')}
                ${this._envField('TAVILY_API_KEYS', 'Tavily 搜索 Key', 'password', '在 app.tavily.com 获取，可选')}
              </div>

              <!-- 邮件 子区块 -->
              <p class="text-xs font-semibold text-gray-500 uppercase tracking-wide mt-5 mb-3">Email</p>
              <div class="space-y-4">
                ${this._envField('EMAIL_SENDER',    '发件人邮箱',   'email',    '')}
                ${this._envField('EMAIL_PASSWORD',  '邮箱授权码',   'password', '非登录密码，在邮箱设置中生成')}
                ${this._envField('EMAIL_RECEIVERS', '收件人邮箱',   'text',     '多个用逗号分隔')}
              </div>

              <!-- 底部按钮 & 状态 -->
              <div class="flex items-center gap-3 mt-5 pt-4 border-t border-gray-100">
                <button id="btn-save-env"
                  class="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 active:scale-95 transition-all">
                  💾 保存到本地 .env
                </button>
                <span id="env-status" class="text-xs text-gray-400">正在读取...</span>
              </div>

            </div>
          </details>
        </div>
      </div>`;

    this._bindToggle('cfg-market-review-btn', 'cfg-market-thumb', 'cfg-market-review',
      (on) => {
        const lbl = this._c.querySelector('#cfg-market-label');
        if (lbl) lbl.textContent = on ? '已启用' : '已关闭';
        this._s.set('market_review_enabled', on);
      });

    this._bindToggle('cfg-force-btn', 'cfg-force-thumb', 'cfg-force',
      (on) => { this._s.set('force_run', on); });

    this._bindToggle('cfg-openhtml-btn', 'cfg-openhtml-thumb', 'cfg-openhtml',
      (on) => {
        const lbl = this._c.querySelector('#cfg-openhtml-label');
        if (lbl) lbl.textContent = on ? '已启用' : '已关闭';
        this._s.set('auto_open_html', on);
      });

    this._bindInput('cfg-report-type',   'report_type');
    this._bindInput('cfg-llm-mode',      'llm_note_mode');
    this._bindInput('cfg-market-region', 'market_review_region');
    this._bindInput('cfg-workers',       'max_workers',    v => Math.max(1, Math.min(5, +v || 1)));
    this._bindInput('cfg-delay',         'analysis_delay', v => Math.max(0, +v || 0));
    this._bindInput('cfg-email-prefix',  'email.subject_prefix');

    this._s.subscribe(state => this._sync(state));

    this._c.querySelector('#btn-save-env')
      ?.addEventListener('click', () => this._saveEnv());

    this._loadEnvStatus();
  }

  _row(label, control, hint = '') {
    return `
      <div class="flex items-center justify-between px-5 py-3.5 gap-4">
        <div class="flex-shrink-0">
          <p class="text-sm font-medium text-gray-700">${label}</p>
          ${hint ? `<p class="text-xs text-gray-400 mt-0.5">${hint}</p>` : ''}
        </div>
        <div class="flex-shrink-0">${control}</div>
      </div>`;
  }

  _bindToggle(btnId, thumbId, hiddenId, onChange) {
    const btn    = this._c.querySelector(`#${btnId}`);
    const thumb  = this._c.querySelector(`#${thumbId}`);
    const hidden = this._c.querySelector(`#${hiddenId}`);
    if (!btn) return;
    const setOn = (on) => {
      hidden.checked = on;
      btn.setAttribute('aria-checked', String(on));
      btn.classList.toggle('bg-blue-500', on);
      btn.classList.toggle('bg-gray-200', !on);
      thumb.classList.toggle('translate-x-5', on);
      thumb.classList.toggle('translate-x-0', !on);
      onChange(on);
    };
    btn.addEventListener('click', () => setOn(!hidden.checked));
  }

  _bindInput(id, key, xf = v => v) {
    const el = this._c.querySelector(`#${id}`);
    if (!el) return;
    el.addEventListener('input', () => this._s.set(key, xf(el.value)));
  }

  _sync(state) {
    const sv = (id, val) => { const el = this._c.querySelector(`#${id}`); if (el) el.value = val ?? ''; };
    sv('cfg-report-type',   state.report_type);
    sv('cfg-llm-mode',      state.llm_note_mode || 'batch');
    sv('cfg-market-region', state.market_review_region);
    sv('cfg-workers',       state.max_workers);
    sv('cfg-delay',         state.analysis_delay);
    sv('cfg-email-prefix',  state.email?.subject_prefix ?? 'A股智能分析');

    this._applyToggle('cfg-market-review-btn', 'cfg-market-thumb', 'cfg-market-review',
      !!state.market_review_enabled,
      (on) => { const l = this._c.querySelector('#cfg-market-label'); if (l) l.textContent = on ? '已启用' : '已关闭'; });
    this._applyToggle('cfg-force-btn', 'cfg-force-thumb', 'cfg-force', !!state.force_run);
    this._applyToggle('cfg-openhtml-btn', 'cfg-openhtml-thumb', 'cfg-openhtml',
      state.auto_open_html !== false,
      (on) => { const l = this._c.querySelector('#cfg-openhtml-label'); if (l) l.textContent = on ? '已启用' : '已关闭'; });
  }

  _applyToggle(btnId, thumbId, hiddenId, on, sideEffect) {
    const btn    = this._c.querySelector(`#${btnId}`);
    const thumb  = this._c.querySelector(`#${thumbId}`);
    const hidden = this._c.querySelector(`#${hiddenId}`);
    if (!btn) return;
    hidden.checked = on;
    btn.setAttribute('aria-checked', String(on));
    btn.classList.toggle('bg-blue-500', on);
    btn.classList.toggle('bg-gray-200', !on);
    thumb.classList.toggle('translate-x-5', on);
    thumb.classList.toggle('translate-x-0', !on);
    sideEffect?.(on);
  }

  /** 渲染单个 .env 字段行 */
  _envField(key, label, type = 'text', help = '') {
    return `
      <div>
        <label class="block text-sm font-medium text-gray-700 mb-1">${label}</label>
        <input type="${type}" id="env-${key}" autocomplete="off" spellcheck="false"
          class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-400 focus:border-transparent font-mono"
          placeholder="${key}" />
        ${help ? `<p class="text-xs text-gray-400 mt-1">${help}</p>` : ''}
      </div>`;
  }

  /** 从 /env 读取掩码值填充各 input */
  async _loadEnvStatus() {
    const status = this._c.querySelector('#env-status');
    try {
      const res = await fetch(`${SERVER}/env`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const env = data.env || data;
      const keys = [
        'HAI_BASE_URL','HAI_API_KEY','HAI_MODEL',
        'GEMINI_API_KEY','DEEPSEEK_API_KEY','OPENAI_API_KEY',
        'BOCHA_API_KEYS','TAVILY_API_KEYS',
        'EMAIL_SENDER','EMAIL_PASSWORD','EMAIL_RECEIVERS'
      ];
      keys.forEach(k => {
        const el = this._c.querySelector(`#env-${k}`);
        if (el && env[k]) el.value = env[k];
      });
      if (status) status.textContent = '已从服务器读取';
    } catch (e) {
      if (status) status.textContent = `读取失败：${e.message}（本地服务未启动？）`;
    }
  }

  /** 收集非空 input 值，POST /env 保存 */
  async _saveEnv() {
    const status = this._c.querySelector('#env-status');
    const keys = [
      'HAI_BASE_URL','HAI_API_KEY','HAI_MODEL',
      'GEMINI_API_KEY','DEEPSEEK_API_KEY','OPENAI_API_KEY',
      'BOCHA_API_KEYS','TAVILY_API_KEYS',
      'EMAIL_SENDER','EMAIL_PASSWORD','EMAIL_RECEIVERS'
    ];
    const payload = {};
    keys.forEach(k => {
      const el = this._c.querySelector(`#env-${k}`);
      // 掩码值（含 *）不回传，避免把掩码写进 .env
      if (el && el.value.trim() && !el.value.includes('*')) payload[k] = el.value.trim();
    });
    if (status) status.textContent = '保存中...';
    try {
      const res = await fetch(`${SERVER}/env`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      this._t?.show('已保存到 .env，分析将自动使用', 'success');
      if (status) status.textContent = '保存成功';
    } catch (e) {
      this._t?.show(`保存失败：${e.message}（请确认本地服务已启动）`, 'error');
      if (status) status.textContent = `保存失败：${e.message}`;
    }
  }
}
