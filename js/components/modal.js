/**
 * components/modal.js — 保存配置弹窗
 */

import { SERVER } from '../config.js';

class Modal {
  constructor() {
    this._el     = null;
    this._store  = null;
    this._toast  = null;
    this._online = false;
  }

  init(store, serverUrl, toast) {
    this._store = store;
    this._toast = toast;
    this._el = document.getElementById('modal');

    document.getElementById('modal-bg')?.addEventListener('click', () => this.hide());
    document.getElementById('modal-close')?.addEventListener('click', () => this.hide());
    document.getElementById('modal-close2')?.addEventListener('click', () => this.hide());
    document.addEventListener('keydown', e => { if (e.key === 'Escape') this.hide(); });

    // 复制按钮
    document.getElementById('modal-copy')?.addEventListener('click', async () => {
      const ta = document.getElementById('modal-json');
      try {
        await navigator.clipboard.writeText(ta.value);
        this._toast.show('已复制到剪贴板', 'success');
      } catch { ta.select(); }
    });

    // 直接保存按钮
    document.getElementById('modal-btn-save')?.addEventListener('click', () => this._directSave());
  }

  setServerOnline(online) {
    this._online = online;
    this._refreshSaveBtn();
  }

  show(json) {
    document.getElementById('modal-json').value = json;
    document.getElementById('modal-save-msg')?.classList.add('hidden');
    this._refreshSaveBtn();
    this._el?.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
  }

  hide() {
    this._el?.classList.add('hidden');
    document.body.style.overflow = '';
  }

  _refreshSaveBtn() {
    const btn  = document.getElementById('modal-btn-save');
    const hint = document.getElementById('modal-server-hint');
    if (!btn) return;
    if (this._online) {
      btn.disabled = false;
      btn.className = 'flex-shrink-0 px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors bg-green-600 hover:bg-green-700 text-white cursor-pointer';
      btn.textContent = '💾 保存';
      if (hint) hint.classList.add('hidden');
    } else {
      btn.disabled = true;
      btn.className = 'flex-shrink-0 px-3 py-1.5 rounded-lg text-xs font-semibold bg-gray-200 text-gray-400 cursor-not-allowed';
      btn.textContent = '保存（需服务）';
      if (hint) hint.classList.remove('hidden');
    }
  }

  async _directSave() {
    const json = document.getElementById('modal-json').value;
    const btn  = document.getElementById('modal-btn-save');
    const msg  = document.getElementById('modal-save-msg');
    btn.disabled = true;
    btn.textContent = '⏳ 保存中...';
    try {
      const res  = await fetch(`${SERVER}/save`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: json,
        signal: AbortSignal.timeout(5000),
      });
      const data = await res.json();
      if (data.ok) {
        msg.className = 'mt-2 text-xs text-green-700 bg-green-100 rounded-lg px-2 py-1';
        msg.textContent = '✅ 已保存到 config/watchlist.json';
        msg.classList.remove('hidden');
        btn.textContent = '✅ 已保存';
        setTimeout(() => { btn.textContent = '💾 保存'; btn.disabled = false; }, 2000);
      } else throw new Error(data.error);
    } catch (e) {
      msg.className = 'mt-2 text-xs text-red-600 bg-red-50 rounded-lg px-2 py-1';
      msg.textContent = `❌ 保存失败：${e.message}`;
      msg.classList.remove('hidden');
      btn.textContent = '💾 保存';
      btn.disabled = false;
    }
  }
}

export const modal = new Modal();
