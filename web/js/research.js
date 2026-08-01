/* research.js — 研究 TAB (P1 政策影响查询, 独立松耦合, 不动交易明细/回测)
 *
 * 输入票代码 → /api/research/policy_impact → 渲染政策影响列表(方向徽章+标题+证据)
 * 松耦合: API 失败/票不在 A 层 → 友好提示, 不影响其他页
 * 后续 P2 Obsidian / P3 大脑也挂这页
 */
(function () {
  function init() {
    var btn = document.getElementById('researchStockBtn');
    var input = document.getElementById('researchStockInput');
    var result = document.getElementById('researchPolicyResult');
    if (!btn || !input || !result) return;
    btn.addEventListener('click', queryPolicy);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') queryPolicy(); });

    function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

    function queryPolicy() {
      var stock = input.value.trim();
      if (!stock) { result.innerHTML = '<span style="color:var(--up)">请输入股票代码</span>'; return; }
      result.innerHTML = '<span style="color:var(--text2)">查询中...</span>';
      fetch('/api/research/policy_impact?stock=' + encodeURIComponent(stock))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var impacts = d.impacts || [];
          if (!impacts.length) {
            result.innerHTML = '<div style="padding:12px;background:var(--surface);border-radius:6px;color:var(--text2)">[ ' + esc(stock) + ' ] 暂无政策影响记录。<br>可能原因: ① 票不在 A 层行业图谱(ETF/新股) ② 政策库未覆盖该行业 ③ 该行业确实无相关政策</div>';
            return;
          }
          var dirColor = function (dir) { return dir === '利好' ? 'var(--up)' : dir === '利空' ? 'var(--down)' : 'var(--text2)'; };
          var strengthBg = function (s) { return s === 'strong' ? 'var(--accent)' : s === 'medium' ? 'var(--pending)' : 'var(--surface)'; };
          result.innerHTML =
            '<div style="margin-bottom:10px;color:var(--text2);font-size:11px">[ ' + esc(stock) + ' ] 共 ' + impacts.length + ' 条政策影响 (按力度排序)</div>' +
            impacts.map(function (i) {
              return '<div style="padding:10px 12px;border:1px solid var(--border);border-radius:6px;margin-bottom:8px;background:var(--card)">' +
                '<div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap">' +
                  '<span style="color:' + dirColor(i.direction) + ';font-weight:600;font-size:12px">' + esc(i.direction) + '</span>' +
                  '<span style="padding:1px 6px;border-radius:4px;font-size:10px;background:' + strengthBg(i.strength) + ';color:#fff">' + esc(i.strength) + '</span>' +
                  '<span style="font-size:12px;color:var(--text)">' + esc(i.title) + '</span>' +
                '</div>' +
                '<div style="font-size:11px;color:var(--text2);padding-left:8px;border-left:2px solid var(--accent)">证据: ' + esc(i.evidence) + '</div>' +
              '</div>';
            }).join('');
        })
        .catch(function (e) {
          result.innerHTML = '<div style="padding:12px;color:var(--up)">查询失败: ' + esc(String(e)) + '<br>(可能 server 未启动或未加载新 API, 重启 server.py)</div>';
        });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
