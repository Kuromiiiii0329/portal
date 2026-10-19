/**
 * components/toast.js — 轻量通知条
 */
class Toast {
  constructor() { this._el = null; this._timer = null; }
  _el_() {
    if (!this._el) this._el = document.getElementById('toast');
    return this._el;
  }
  show(msg, type = 'info', ms = 2400) {
    const el = this._el_();
    if (!el) return;
    const colors = { success: 'bg-green-600', error: 'bg-red-600', warning: 'bg-amber-500', info: 'bg-gray-800' };
    el.removeAttribute('class');
    el.className = `fixed bottom-20 left-1/2 -translate-x-1/2 z-50 px-4 py-2 rounded-xl text-white text-xs font-medium shadow-lg whitespace-nowrap ${colors[type] || colors.info}`;
    el.textContent = msg;
    el.style.display = '';
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => { el.style.display = 'none'; }, ms);
  }
}
export const toast = new Toast();
