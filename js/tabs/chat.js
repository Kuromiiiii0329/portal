/**
 * tabs/chat.js — 🤖 AI 对话（交互式 multi-agent 问答，选项A）
 *
 * 通过 POST /chat 触发 server 端的 AgentOrchestrator（Technical→Intel→Decision 多轮），
 * 复用现有 SSE 通道 /run/stream/<task_id> 显示各 agent 阶段进度，done 后取 /run/report/<id>
 * 作为 assistant 气泡渲染。
 *
 * session_id：前端生成并持久化到 localStorage（key: dsa_chat_session），保证多轮连续。
 * 纯静态模式（server 离线）时优雅提示"需启动本地服务"。
 */

import { SERVER } from '../config.js';
const SESSION_KEY = 'dsa_chat_session';

export class ChatTab {
  constructor(container, store, toast) {
    this._c = container;
    this._s = store;
    this._t = toast;
    this._online = false;
    this._busy = false;
    this._sse = null;
  }

  init() {
    this._c.innerHTML = `
      <div class="flex flex-col h-full">
        <!-- 股票上下文（可选）-->
        <div class="flex gap-2 mb-2">
          <input id="chat-code" placeholder="股票代码(可选)"
            class="w-28 px-2 py-1.5 text-xs border border-gray-200 rounded-lg focus:outline-none focus:border-blue-400"/>
          <input id="chat-name" placeholder="名称(可选)"
            class="flex-1 px-2 py-1.5 text-xs border border-gray-200 rounded-lg focus:outline-none focus:border-blue-400"/>
          <button id="chat-clear"
            class="px-3 py-1.5 text-xs rounded-lg bg-gray-100 hover:bg-gray-200 text-gray-600">清空</button>
        </div>

        <!-- 消息流 -->
        <div id="chat-stream"
          class="flex-1 overflow-y-auto space-y-3 p-2 bg-gray-50 rounded-xl min-h-[280px] max-h-[52vh]">
          <div class="text-center text-xs text-gray-400 py-8">
            🤖 向 AI 投研助手提问，例如：<br/>
            "帮我看看这只票现在能不能买"、"结合技术面和风险分析一下"
          </div>
        </div>

        <!-- 输入区 -->
        <div id="chat-hint" class="text-[11px] text-amber-600 mt-2 mb-1 hidden">
          ⚠️ AI 对话需启动本地服务（python portal/server.py）
        </div>
        <div class="flex gap-2 mt-2">
          <textarea id="chat-input" rows="2" placeholder="输入问题，Enter 发送 / Shift+Enter 换行"
            class="flex-1 px-3 py-2 text-xs border border-gray-200 rounded-xl resize-none focus:outline-none focus:border-blue-400"></textarea>
          <button id="chat-send"
            class="px-4 rounded-xl text-xs font-bold bg-blue-600 hover:bg-blue-700 text-white
                   shadow-sm shadow-blue-200 active:scale-[.99] disabled:opacity-50 disabled:cursor-not-allowed">
            发送
          </button>
        </div>
      </div>
    `;

    this._stream = this._c.querySelector('#chat-stream');
    this._input  = this._c.querySelector('#chat-input');
    this._sendBtn = this._c.querySelector('#chat-send');

    this._sendBtn.addEventListener('click', () => this._send());
    this._input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._send(); }
    });
    this._c.querySelector('#chat-clear').addEventListener('click', () => this._clear());

    this._syncOnlineUI();
  }

  // 由 app.js 的 _switchTab 调用（若注册），保持与 RunTab 一致
  setServerStatus(online) {
    this._online = online;
    this._syncOnlineUI();
  }

  _syncOnlineUI() {
    const hint = this._c.querySelector('#chat-hint');
    if (hint) hint.classList.toggle('hidden', this._online);
  }

  _sessionId() {
    let sid = null;
    try { sid = localStorage.getItem(SESSION_KEY); } catch {}
    if (!sid) {
      sid = (crypto?.randomUUID?.() ? `sess_${crypto.randomUUID()}` : `sess_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
      try { localStorage.setItem(SESSION_KEY, sid); } catch {}
    }
    return sid;
  }

  _clear() {
    try { localStorage.removeItem(SESSION_KEY); } catch {}
    this._stream.innerHTML = `<div class="text-center text-xs text-gray-400 py-8">🆕 已开始新对话</div>`;
    this._t?.show?.('已开始新对话', 'info');
  }

  _bubble(role, html) {
    const wrap = document.createElement('div');
    const isUser = role === 'user';
    wrap.className = `flex ${isUser ? 'justify-end' : 'justify-start'}`;
    wrap.innerHTML = `
      <div class="${isUser ? 'bg-blue-600 text-white' : 'bg-white text-gray-800 border border-gray-200'}
                  max-w-[85%] px-3 py-2 rounded-2xl text-xs leading-relaxed shadow-sm
                  ${isUser ? 'rounded-br-sm' : 'rounded-bl-sm'}">${html}</div>`;
    // 首次发消息时清掉占位提示
    const placeholder = this._stream.querySelector('.text-center');
    if (placeholder) placeholder.remove();
    this._stream.appendChild(wrap);
    this._stream.scrollTop = this._stream.scrollHeight;
    return wrap.firstElementChild;
  }

  async _send() {
    if (this._busy) return;
    const message = this._input.value.trim();
    if (!message) return;
    if (!this._online) {
      this._t?.show?.('AI 对话需启动本地服务', 'error');
      return;
    }

    this._busy = true;
    this._sendBtn.disabled = true;
    this._bubble('user', this._esc(message));
    this._input.value = '';

    const code = this._c.querySelector('#chat-code').value.trim().toUpperCase();
    const name = this._c.querySelector('#chat-name').value.trim();

    // 助手"思考中"气泡（先展示 stage 进度，done 后替换成最终回答）
    const thinking = this._bubble('assistant', '<span class="text-gray-400">🤖 思考中…</span>');
    const stages = [];

    let taskId, sessionId;
    try {
      const res = await fetch(`${SERVER}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, session_id: this._sessionId(), stock_code: code, stock_name: name || code }),
      });
      const d = await res.json();
      if (!d.ok) throw new Error(d.error || '请求失败');
      taskId = d.task_id;
      sessionId = d.session_id;
      if (sessionId) { try { localStorage.setItem(SESSION_KEY, sessionId); } catch {} }
    } catch (e) {
      thinking.innerHTML = `<span class="text-red-500">❌ ${this._esc(e.message)}</span>`;
      this._finish();
      return;
    }

    // 复用现有 SSE 通道
    const sse = new EventSource(`${SERVER}/run/stream/${taskId}`);
    this._sse = sse;

    sse.onmessage = e => {
      try {
        const d = JSON.parse(e.data);
        if (d.log) {
          stages.push(d.log);
          // 只展示最近几条 stage 进度
          const recent = stages.slice(-4).map(s => this._esc(s)).join('<br/>');
          thinking.innerHTML = `<span class="text-gray-400">🤖 ${recent}</span>`;
          this._stream.scrollTop = this._stream.scrollHeight;
        }
      } catch {}
    };

    sse.addEventListener('done', async () => {
      sse.close(); this._sse = null;
      try {
        const res = await fetch(`${SERVER}/run/report/${taskId}`);
        const d = await res.json();
        const answer = (d.report || '').trim();
        thinking.innerHTML = answer ? this._md(answer) : '<span class="text-gray-400">（无回复）</span>';
      } catch (e) {
        thinking.innerHTML = `<span class="text-red-500">❌ 获取回复失败：${this._esc(e.message)}</span>`;
      }
      this._stream.scrollTop = this._stream.scrollHeight;
      this._finish();
    });

    sse.onerror = () => {
      sse.close(); this._sse = null;
      // 连接断开后兜底拉一次结果
      setTimeout(async () => {
        try {
          const res = await fetch(`${SERVER}/run/report/${taskId}`);
          const d = await res.json();
          const answer = (d.report || '').trim();
          if (answer) thinking.innerHTML = this._md(answer);
        } catch {}
        this._finish();
      }, 600);
    };
  }

  _finish() {
    this._busy = false;
    this._sendBtn.disabled = false;
    this._input.focus();
  }

  _esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // 轻量 markdown 渲染（与 run.js 的 _md 同款风格）
  _md(md) {
    const esc = this._esc;
    return md.split('\n').map(l => {
      if (/^### (.+)/.test(l)) return `<h3 class="font-bold mt-2 mb-1 text-xs">${esc(l.slice(4))}</h3>`;
      if (/^## (.+)/.test(l))  return `<h2 class="font-bold mt-2 mb-1 text-sm">${esc(l.slice(3))}</h2>`;
      if (/^# (.+)/.test(l))   return `<h1 class="font-bold mt-2 mb-1 text-sm">${esc(l.slice(2))}</h1>`;
      if (/^---+$/.test(l))    return '<hr class="my-2 border-gray-200"/>';
      if (/^[-*] (.+)/.test(l)) return `<p class="ml-3">• ${esc(l.slice(2))}</p>`;
      if (!l.trim()) return '<br/>';
      let h = esc(l);
      h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
      h = h.replace(/`(.+?)`/g, '<code class="bg-gray-100 px-1 rounded text-blue-700">$1</code>');
      return `<p>${h}</p>`;
    }).join('\n');
  }
}
