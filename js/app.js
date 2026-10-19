/**
 * app.js — 主入口：Tab 路由 + 服务状态轮询 + 底栏 + Modal
 */
import { store }       from './store.js';
import { SERVER }      from './config.js';
import { WatchlistTab } from './tabs/watchlist.js';
import { SettingsTab }  from './tabs/settings.js';
import { RunTab }       from './tabs/run.js';
import { GuideTab }     from './tabs/guide.js';
import { ChatTab }      from './tabs/chat.js';
import { toast }        from './components/toast.js';
import { modal }        from './components/modal.js';

const TABS = [
  { id: 'run',       icon: '▶',  label: '立即运行', Cls: RunTab },
  { id: 'chat',      icon: '🤖', label: 'AI 对话',  Cls: ChatTab },
  { id: 'watchlist', icon: '☰',  label: '选股',     Cls: WatchlistTab },
  { id: 'settings',  icon: '⚙', label: '设置',     Cls: SettingsTab },
  { id: 'guide',     icon: '?',  label: '说明',     Cls: GuideTab },
];

class App {
  constructor() {
    this._instances = {};
    this._activeId  = 'run';
    this._online    = false;
  }

  init() {
    this._buildNav();
    this._buildPanels();
    modal.init(store, SERVER, toast);
    this._bindSave();
    this._bindFooter();
    this._pollServer();
    this._restoreTab();
  }

  // ── Nav ─────────────────────────────────────────────────
  _buildNav() {
    const nav = document.getElementById('tab-nav');
    TABS.forEach(({ id, icon, label }) => {
      const btn = document.createElement('button');
      btn.id = `nav-${id}`;
      btn.className = 'nav-tab flex-1 flex items-center justify-center gap-1 py-1.5 text-xs font-medium text-gray-500 rounded-lg';
      btn.innerHTML = `<span>${icon}</span><span>${label}</span>`;
      btn.addEventListener('click', () => this._switchTab(id));
      nav.appendChild(btn);
    });
  }

  _buildPanels() {
    const content = document.getElementById('tab-content');
    TABS.forEach(({ id, Cls }) => {
      const panel = document.createElement('div');
      panel.id = `panel-${id}`;
      panel.className = 'tab-panel';
      content.appendChild(panel);
      const inst = new Cls(panel, store, toast);
      inst.init();
      this._instances[id] = inst;
    });
  }

  _switchTab(id) {
    this._activeId = id;
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
    document.getElementById(`panel-${id}`)?.classList.add('active');
    document.getElementById(`nav-${id}`)?.classList.add('active');
    try { sessionStorage.setItem('dsa_tab', id); } catch {}
  }

  _restoreTab() {
    const saved = (() => { try { return sessionStorage.getItem('dsa_tab'); } catch {} })();
    this._switchTab(TABS.some(t => t.id === saved) ? saved : 'run');
  }

  // ── Server poll ─────────────────────────────────────────
  async _pollServer() {
    const check = async () => {
      let ok = false;
      try {
        const r = await fetch(`${SERVER}/health`, { signal: AbortSignal.timeout(1500) });
        ok = r.ok;
      } catch {}
      if (ok !== this._online) {
        this._online = ok;
        this._updateStatusUI(ok);
        // 通知 RunTab / ChatTab
        this._instances['run']?.setServerStatus?.(ok);
        this._instances['chat']?.setServerStatus?.(ok);
      }
    };
    await check();
    setInterval(check, 5000);
  }

  _updateStatusUI(online) {
    const dot   = document.getElementById('status-dot');
    const label = document.getElementById('status-label');
    if (!dot || !label) return;
    dot.className   = `status-dot ${online ? 'online' : 'offline'}`;
    label.textContent = online ? '本地服务在线' : '纯静态模式';
    label.className   = `text-xs ${online ? 'text-green-600 font-medium' : 'text-gray-400'}`;
    // 通知 modal
    modal.setServerOnline(online);
  }

  // ── Footer ───────────────────────────────────────────────
  _bindFooter() {
    store.subscribe(() => {
      const el = document.getElementById('footer-time');
      if (!el) return;
      el.textContent = `上次编辑 ${new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`;
    });
  }

  _bindSave() {
    document.getElementById('btn-save')?.addEventListener('click', () => {
      modal.show(store.toJSON());
    });
  }
}

document.addEventListener('DOMContentLoaded', () => new App().init());
