/**
 * tabs/guide.js — 使用说明（纯静态）
 */
export class GuideTab {
  constructor(container, store, toast) { this._c = container; }

  init() {
    this._c.innerHTML = `
      <div class="max-w-2xl mx-auto space-y-6">
        <div>
          <h2 class="text-base font-semibold text-gray-900">使用说明</h2>
          <p class="text-xs text-gray-500 mt-0.5">两种使用模式，按需选择</p>
        </div>

        <!-- 模式说明 -->
        <div class="grid grid-cols-2 gap-3">
          <div class="bg-blue-50 border border-blue-100 rounded-2xl p-4">
            <p class="text-sm font-semibold text-blue-800 mb-1">🌐 纯静态模式</p>
            <p class="text-xs text-blue-700 leading-relaxed">
              管理自选股、调整参数、导出配置文件。<br/>
              可直接通过 GitHub Pages 访问，无需启动任何程序。
            </p>
            <div class="mt-2 space-y-1">
              <p class="text-xs text-blue-600">✓ 选股管理</p>
              <p class="text-xs text-blue-600">✓ 参数设置</p>
              <p class="text-xs text-blue-600">✓ 复制 JSON 配置</p>
              <p class="text-xs text-blue-600">✓ GitHub Action 每日分析</p>
            </div>
          </div>
          <div class="bg-green-50 border border-green-100 rounded-2xl p-4">
            <p class="text-sm font-semibold text-green-800 mb-1">⚡ 本地增强模式</p>
            <p class="text-xs text-green-700 leading-relaxed">
              点击「直接保存」或「立即分析」时自动激活，无需手动启动。
            </p>
            <div class="mt-2 space-y-1">
              <p class="text-xs text-green-600">✓ 直接写入配置文件</p>
              <p class="text-xs text-green-600">✓ 立即触发个股深度分析</p>
              <p class="text-xs text-green-600">✓ 实时查看分析日志</p>
              <p class="text-xs text-green-600">✓ 结构化双维度报告</p>
            </div>
          </div>
        </div>

        <!-- 步骤 -->
        <div class="bg-white border border-gray-100 rounded-2xl shadow-sm divide-y divide-gray-50">
          ${[
            ['1', '配置 GitHub Secrets', `
              仓库 <strong>Settings → Secrets and variables → Actions</strong> 添加：
              <div class="mt-2 overflow-x-auto">
                <table class="w-full text-xs border-collapse">
                  <thead><tr class="bg-gray-50">
                    <th class="text-left px-2 py-1.5 border border-gray-100">变量名</th>
                    <th class="text-left px-2 py-1.5 border border-gray-100">说明</th>
                    <th class="px-2 py-1.5 border border-gray-100">必填</th>
                  </tr></thead>
                  <tbody>
                    ${[
                      ['EMAIL_SENDER','发件人邮箱','✅'],
                      ['EMAIL_PASSWORD','SMTP 授权码','✅'],
                      ['EMAIL_RECEIVERS','收件人，逗号分隔','✅'],
                      ['GEMINI_API_KEY','Google Gemini Key（免费可用）','✅ AI必填其一'],
                      ['DEEPSEEK_API_KEY','DeepSeek Key','可选'],
                      ['OPENAI_API_KEY','OpenAI Key','可选'],
                      ['TUSHARE_TOKEN','Tushare Pro Token（提升数据质量）','可选'],
                    ].map(([n,d,r]) => `<tr>
                      <td class="px-2 py-1 border border-gray-100 font-mono text-blue-700">${n}</td>
                      <td class="px-2 py-1 border border-gray-100 text-gray-600">${d}</td>
                      <td class="px-2 py-1 border border-gray-100 text-center">${r}</td>
                    </tr>`).join('')}
                  </tbody>
                </table>
              </div>`],
            ['2', '配置自选股和参数', `
              在「自选股」Tab 添加股票代码，在「设置」Tab 调整参数。<br/>
              点击底部「保存配置」→「复制 JSON」→ 提交到仓库 <code class="bg-gray-100 px-1 rounded">config/watchlist.json</code>`],
            ['3', '启用 GitHub Pages（可选）', `
              仓库 Settings → Pages → Source: main，Folder: /portal<br/>
              访问 <code class="bg-gray-100 px-1 rounded">https://用户名.github.io/仓库名/</code>`],
            ['4', '等待每日自动分析', `
              每个工作日北京时间 18:00 自动运行，结果发送到配置的邮箱。<br/>
              也可在 Actions → <strong>Portal 每日股票分析</strong> 手动触发。`],
            ['5', '本地立即分析（可选）', `
              安装 Python 依赖后，直接在「立即运行」Tab 点击分析按钮，<br/>
              首次点击会自动尝试启动本地服务，无需手动操作。<br/>
              <code class="bg-gray-100 px-1 rounded text-xs">pip install -r requirements.txt</code>`],
          ].map(([n, title, body]) => `
            <div class="px-5 py-4 flex gap-4">
              <div class="flex-shrink-0 w-6 h-6 rounded-full bg-blue-600 text-white text-xs font-bold flex items-center justify-center mt-0.5">${n}</div>
              <div>
                <p class="text-sm font-semibold text-gray-800 mb-1.5">${title}</p>
                <div class="text-xs text-gray-600 leading-relaxed">${body}</div>
              </div>
            </div>`).join('')}
        </div>

        <!-- FAQ -->
        <div class="space-y-2">
          <p class="text-xs font-semibold text-gray-500 uppercase tracking-wide">常见问题</p>
          ${[
            ['邮件收不到？', '检查 EMAIL_SENDER/EMAIL_PASSWORD 是否正确，确认 SMTP 服务已开启（QQ邮箱需手动开启 POP3/SMTP）。'],
            ['AI 分析报错？', '确认至少配置了一个 AI API Key（GEMINI_API_KEY 推荐，有免费额度）。'],
            ['东方财富数据源失败？', '正常现象，系统自动切换到 AkShare/新浪备用源。'],
            ['如何修改分析时间？', '修改 .github/workflows/portal-daily-analysis.yml 中的 cron 表达式，UTC = 北京时间 - 8h。'],
          ].map(([q, a]) => `
            <details class="bg-white border border-gray-100 rounded-xl overflow-hidden">
              <summary class="px-4 py-3 text-sm font-medium text-gray-700 cursor-pointer hover:bg-gray-50 flex items-center justify-between">
                <span>${q}</span><span class="chevron text-gray-400 text-xs">▶</span>
              </summary>
              <div class="px-4 pb-3 text-xs text-gray-600 leading-relaxed">${a}</div>
            </details>`).join('')}
        </div>
      </div>`;
  }
}
