/* ═══════════════════════════════════════════════════════════════════════
   卡塔音乐 · 后台管理面板 — Admin Dashboard JS
   长按5秒触发 · 密码验证 · 数据看板 · API 状态 · 访客图表
   ═══════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // ── State ──
  var ADMIN_ACTIVE = false;
  var statsRefreshTimer = null;
  var apiCheckTimer = null;
  var apiCheckActive = false;
  var apiInterval = 86400000; // default: 24 hours

  // API list (matches backend)
  var API_LIST = [
    "myhkw.cn (搜索)",
    "NetEase API",
    "QQ Music API",
    "Kugou API",
    "Kuwo API",
    "Migu API",
    "GDStudio API",
    "Xiageba API",
    "Luckxz",
    "Tonzhon (legacy)",
  ];

  // ── DOM refs (created dynamically) ──
  var $overlay, $shell, $pwdOverlay, $pwdInput, $pwdError;

  // ═════════════════════════════════════════════════════════════════════
  // INIT — Inject admin HTML into the page
  // ═════════════════════════════════════════════════════════════════════
  function injectAdminHTML() {
    var html = '';

    // ── Password modal ──
    html += '<div class="pwd-overlay" id="pwdOverlay">';
    html +=   '<div class="pwd-dialog">';
    html +=     '<span class="pwd-icon">🔐</span>';
    html +=     '<div class="pwd-title">管理员验证</div>';
    html +=     '<div class="pwd-subtitle">请输入管理员密码以进入后台管理系统</div>';
    html +=     '<input type="password" class="pwd-input" id="pwdInput" placeholder="输入密码..." autocomplete="off">';
    html +=     '<div class="pwd-error" id="pwdError"></div>';
    html +=     '<button class="pwd-btn" id="pwdBtn">验证进入</button>';
    html +=     '<button class="pwd-cancel" id="pwdCancel">取消</button>';
    html +=   '</div>';
    html += '</div>';

    // ── Admin overlay ──
    html += '<div class="admin-overlay" id="adminOverlay">';
    html +=   '<div class="admin-shell" id="adminShell">';

    // Header
    html +=     '<div class="admin-header">';
    html +=       '<div class="admin-header-inner">';
    html +=         '<button class="admin-back-btn" id="adminBackBtn">← 返回前台</button>';
    html +=         '<div class="admin-title-group">';
    html +=           '<span class="admin-title-icon">⚙️</span>';
    html +=           '<div class="admin-title">卡塔音乐 · 后台管理<small>Kata Music Analytics Dashboard</small></div>';
    html +=         '</div>';
    html +=         '<div class="admin-header-actions">';
    html +=           '<button class="admin-refresh-btn" id="adminRefreshBtn">';
    html +=             '<span class="admin-refresh-icon">🔄</span> 刷新数据';
    html +=           '</button>';
    html +=         '</div>';
    html +=       '</div>';
    html +=     '</div>';

    // Content
    html +=     '<div class="admin-content" id="adminContent">';
    html +=       loadingHTML();
    html +=     '</div>';

    html +=   '</div>';
    html += '</div>';

    // Append to body
    var container = document.createElement('div');
    container.innerHTML = html;
    while (container.firstChild) {
      document.body.appendChild(container.firstChild);
    }

    // Cache DOM refs
    $overlay = document.getElementById('adminOverlay');
    $shell = document.getElementById('adminShell');
    $pwdOverlay = document.getElementById('pwdOverlay');
    $pwdInput = document.getElementById('pwdInput');
    $pwdError = document.getElementById('pwdError');
  }

  function loadingHTML() {
    return '<div style="text-align:center;padding:80px 20px;color:#595959;">' +
      '<div class="spinner" style="width:36px;height:36px;margin:0 auto 16px"></div>' +
      '<p style="font-size:14px">正在加载后台数据...</p></div>';
  }

  // ═════════════════════════════════════════════════════════════════════
  // LOGO CLICK — show password modal
  // ═════════════════════════════════════════════════════════════════════
  function attachLogoTrigger() {
    var logo = document.querySelector('.header-logo');
    if (!logo) {
      setTimeout(attachLogoTrigger, 500);
      return;
    }

    logo.style.cursor = 'pointer';
    logo.style.userSelect = 'none';
    logo.title = '点击进入后台管理';

    logo.addEventListener('click', function (e) {
      if (ADMIN_ACTIVE) return;
      e.preventDefault();
      showPasswordModal();
    });
  }

  // ═════════════════════════════════════════════════════════════════════
  // PASSWORD MODAL
  // ═════════════════════════════════════════════════════════════════════
  function showPasswordModal() {
    $pwdOverlay.classList.add('active');
    $pwdInput.value = '';
    $pwdError.textContent = '';
    setTimeout(function () { $pwdInput.focus(); }, 350);
  }

  function hidePasswordModal() {
    $pwdOverlay.classList.remove('active');
    $pwdInput.value = '';
    $pwdError.textContent = '';
  }

  function verifyPassword() {
    var pwd = $pwdInput.value.trim();
    if (!pwd) {
      $pwdError.textContent = '请输入密码';
      $pwdInput.focus();
      return;
    }

    var btn = document.getElementById('pwdBtn');
    btn.textContent = '验证中...';
    btn.disabled = true;

    // Step 1: Get challenge nonce from server
    fetch('/api/admin/challenge')
      .then(function (r) { return r.json(); })
      .then(function (chal) {
        if (!chal.nonce) {
          throw new Error('无法获取验证挑战');
        }
        var nonce = chal.nonce;

        // Step 2: Compute proof = SHA256(nonce + SHA256(password))
        // The password is hashed twice — never sent in plaintext
        var enc = new TextEncoder();
        // First hash: SHA256(password)
        return crypto.subtle.digest('SHA-256', enc.encode(pwd))
          .then(function (pwdHash) {
            // pwdHash is ArrayBuffer → hex string
            var pwdHex = Array.from(new Uint8Array(pwdHash))
              .map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
            // Second hash: SHA256(nonce + SHA256(password))
            return crypto.subtle.digest('SHA-256', enc.encode(nonce + pwdHex));
          })
          .then(function (proofBuf) {
            var proof = Array.from(new Uint8Array(proofBuf))
              .map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');

            // Step 3: Send proof (not password!) to server
            return fetch('/api/admin/verify', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ proof: proof, nonce: nonce }),
            });
          });
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success) {
          // Store the signed token for subsequent admin API calls
          window._adminToken = data.token;
          $pwdError.textContent = '';
          hidePasswordModal();
          openAdminDashboard();
        } else {
          $pwdError.textContent = data.error || '密码错误';
          $pwdInput.value = '';
          $pwdInput.focus();
          // Shake animation
          var dialog = $pwdOverlay.querySelector('.pwd-dialog');
          dialog.style.transform = 'translateX(-8px)';
          setTimeout(function () { dialog.style.transform = 'translateX(8px)'; }, 80);
          setTimeout(function () { dialog.style.transform = 'translateX(-4px)'; }, 160);
          setTimeout(function () { dialog.style.transform = ''; }, 240);
        }
      })
      .catch(function (e) {
        $pwdError.textContent = '网络错误，请重试';
      })
      .finally(function () {
        btn.textContent = '验证进入';
        btn.disabled = false;
      });
  }

  function attachPasswordEvents() {
    document.getElementById('pwdBtn').addEventListener('click', verifyPassword);
    document.getElementById('pwdCancel').addEventListener('click', hidePasswordModal);
    $pwdInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') verifyPassword();
      if (e.key === 'Escape') hidePasswordModal();
    });
    // Close modal on backdrop click
    $pwdOverlay.addEventListener('click', function (e) {
      if (e.target === $pwdOverlay) hidePasswordModal();
    });
  }

  // ═════════════════════════════════════════════════════════════════════
  // ADMIN DASHBOARD
  // ═════════════════════════════════════════════════════════════════════
  function openAdminDashboard() {
    ADMIN_ACTIVE = true;
    $overlay.classList.add('active');
    document.body.style.overflow = 'hidden';
    loadDashboardData();
  }

  function closeAdminDashboard() {
    ADMIN_ACTIVE = false;
    window._adminToken = null;  // clear session token
    $overlay.classList.remove('active');
    document.body.style.overflow = '';
    if (statsRefreshTimer) {
      clearTimeout(statsRefreshTimer);
      statsRefreshTimer = null;
    }
    if (apiCheckTimer) {
      clearTimeout(apiCheckTimer);
      apiCheckTimer = null;
    }
    apiCheckActive = false;
  }

  function attachDashboardEvents() {
    document.getElementById('adminBackBtn').addEventListener('click', closeAdminDashboard);

    // ESC to close
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && ADMIN_ACTIVE) {
        if ($pwdOverlay.classList.contains('active')) {
          hidePasswordModal();
        } else {
          closeAdminDashboard();
        }
      }
    });

    // Refresh button
    document.getElementById('adminRefreshBtn').addEventListener('click', function () {
      var btn = document.getElementById('adminRefreshBtn');
      btn.classList.add('spinning');
      loadDashboardData();
      checkAPIStatus().finally(function () {
        setTimeout(function () { btn.classList.remove('spinning'); }, 600);
      });
    });

    // API interval selector (delegated — DOM may not exist yet on boot)
    document.addEventListener('change', function (e) {
      if (e.target && e.target.id === 'apiIntervalSelect') {
        apiInterval = parseInt(e.target.value, 10);
        // Reset timer with new interval
        if (apiCheckTimer) clearTimeout(apiCheckTimer);
        if (ADMIN_ACTIVE) scheduleNextAPICheck();
      }
    });

    // Backdrop click to close
    $overlay.addEventListener('click', function (e) {
      if (e.target === $overlay) {
        closeAdminDashboard();
      }
    });
  }

  // ═════════════════════════════════════════════════════════════════════
  // DATA FETCHING
  // ═════════════════════════════════════════════════════════════════════
  // Helper: make authenticated admin API request
  function adminFetch(url, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    if (window._adminToken) {
      opts.headers['Authorization'] = 'Bearer ' + window._adminToken;
    }
    return fetch(url, opts);
  }

  function loadDashboardData() {
    adminFetch('/api/admin/stats')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) {
          showError(data.error);
          return;
        }
        renderDashboard(data);
        // Auto-refresh every 60 seconds
        if (statsRefreshTimer) clearTimeout(statsRefreshTimer);
        statsRefreshTimer = setTimeout(loadDashboardData, 60000);
      })
      .catch(function (e) {
        showError('数据加载失败: ' + e.message);
      });
  }

  function checkAPIStatus() {
    if (apiCheckActive) return Promise.resolve(null);
    apiCheckActive = true;

    var grid = document.getElementById('apiGrid');
    var summary = document.getElementById('apiSummary');
    var spinner = document.getElementById('apiHeaderSpinner');

    // Show header spinner and "检测中" summary
    if (spinner) spinner.style.display = 'inline-block';
    if (summary) { summary.textContent = '检测中...'; summary.className = 'admin-api-summary warn'; }

    // Show existing cards (if any) with "待更新" labels, or init empty cards
    var existingCards = grid ? grid.querySelectorAll('.admin-api-card').length : 0;
    if (!existingCards) {
      initAPICards();
    }
    markAllPending();

    // Fire all checks in parallel, update cards as each completes
    var promises = API_LIST.map(function (name) {
      return adminFetch('/api/admin/api-check-one?name=' + encodeURIComponent(name))
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.name && data.result) {
            updateAPICard(data.name, data.result, 'updated');
          }
          return data;
        })
        .catch(function () {
          updateAPICard(name, { available: false, error: '请求失败', response_ms: 0 }, 'updated');
          return null;
        });
    });

    return Promise.allSettled(promises).then(function () {
      apiCheckActive = false;
      if (spinner) spinner.style.display = 'none';
      // Count results
      var avail = 0, total = 0;
      var cards = grid ? grid.querySelectorAll('.admin-api-card') : [];
      cards.forEach(function (card) {
        total++;
        if (card.querySelector('.admin-api-dot.online')) avail++;
      });
      var ratio = avail + '/' + total;
      var ratioNum = total > 0 ? avail / total : 0;
      if (summary) {
        summary.textContent = '可用: ' + ratio;
        summary.className = 'admin-api-summary ' + (ratioNum >= 0.75 ? 'good' : (ratioNum >= 0.4 ? 'warn' : 'bad'));
      }
      // Remove all labels
      clearAllLabels();
      scheduleNextAPICheck();
      return { available: avail, total: total, ratio: ratio };
    });
  }

  // API → homepage URL mapping (matches backend APIS_TO_CHECK)
  var API_URLS = {
    "myhkw.cn (搜索)":    "http://s.myhkw.cn/",
    "NetEase API":        "https://music.163.com/",
    "QQ Music API":       "https://y.qq.com/",
    "Kugou API":          "https://www.kugou.com/",
    "Kuwo API":           "http://www.kuwo.cn/",
    "Migu API":           "https://music.migu.cn/",
    "GDStudio API":       "https://gdstudio.xyz/",
    "Xiageba API":        "https://xiageba.liumingye.cn/",
    "Luckxz":             "https://luckxz.com/",
    "Tonzhon (legacy)":   "https://tonzhon.whamon.com/",
  };

  function initAPICards() {
    var grid = document.getElementById('apiGrid');
    if (!grid) return;
    grid.innerHTML = API_LIST.map(function (name) {
      var url = API_URLS[name] || '';
      return '<div class="admin-api-card" data-api="' + escHTML(name) + '"' +
        (url ? ' data-url="' + escHTML(url) + '" title="点击跳转到 ' + escHTML(name) + '"' : '') + '>' +
        '<span class="admin-api-dot checking"></span>' +
        '<div class="admin-api-info">' +
          '<div class="admin-api-name">' + escHTML(name) + '</div>' +
          '<div class="admin-api-ms">等待检测...</div>' +
        '</div>' +
        (url ? '<span class="admin-api-link-hint">↗</span>' : '') +
        '<span class="admin-api-label pending">待更新</span>' +
      '</div>';
    }).join('');

    // Delegate click to open API homepage in new tab
    grid.onclick = function (e) {
      var card = e.target.closest('.admin-api-card');
      if (!card) return;
      var targetUrl = card.dataset.url;
      if (targetUrl) window.open(targetUrl, '_blank');
    };
  }

  function markAllPending() {
    var cards = document.querySelectorAll('#apiGrid .admin-api-card');
    cards.forEach(function (card) {
      var label = card.querySelector('.admin-api-label');
      if (label) { label.textContent = '待更新'; label.className = 'admin-api-label pending'; }
    });
  }

  function clearAllLabels() {
    var cards = document.querySelectorAll('#apiGrid .admin-api-label');
    cards.forEach(function (label) {
      label.style.display = 'none';
    });
  }

  function updateAPICard(name, result, status) {
    var card = document.querySelector('#apiGrid .admin-api-card[data-api="' + name + '"]');
    if (!card) return;

    var isUp = result.available;
    var ms = result.response_ms || 0;
    var msText = ms > 0 ? ms + 'ms' : (result.error || '超时');

    // Update dot
    var dot = card.querySelector('.admin-api-dot');
    if (dot) { dot.className = 'admin-api-dot ' + (isUp ? 'online' : 'offline'); }

    // Update info
    var info = card.querySelector('.admin-api-ms');
    if (info) { info.textContent = isUp ? msText : (result.error || '不可用'); }

    // Update label
    var label = card.querySelector('.admin-api-label');
    if (label) {
      if (status === 'updated') {
        label.textContent = '已更新';
        label.className = 'admin-api-label updated';
      }
    }
  }

  function scheduleNextAPICheck() {
    if (apiCheckTimer) clearTimeout(apiCheckTimer);
    if (apiInterval > 0) {
      apiCheckTimer = setTimeout(function () {
        if (ADMIN_ACTIVE) checkAPIStatus();
      }, apiInterval);
    }
  }

  function showError(msg) {
    document.getElementById('adminContent').innerHTML =
      '<div style="text-align:center;padding:60px 20px;color:#ffa39e;">' +
      '<p style="font-size:16px">⚠️ ' + escHTML(msg) + '</p>' +
      '<button onclick="location.reload()" style="margin-top:16px;padding:8px 20px;background:#fa8c16;color:#fff;border:none;border-radius:16px;cursor:pointer">重试</button>' +
      '</div>';
  }

  function escHTML(s) {
    var d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }

  // ═════════════════════════════════════════════════════════════════════
  // RENDER
  // ═════════════════════════════════════════════════════════════════════
  function renderDashboard(d) {
    var v = d.visitors || {};
    var dl = d.downloads || {};
    var top = d.top_songs || [];
    var ch = d.top_channels || [];
    var trend = d.trend || [];
    var recent = d.recent_downloads || [];

    var html = '';

    // ── Stats cards ──
    html += '<div class="admin-section-title">📊 访客概览</div>';
    html += '<div class="admin-stats-row">';
    html += statCard('👤', '今日访客', v.today || 0, '今日总访问 ' + (v.today_total || 0) + ' 次', '1');
    html += statCard('📅', '本月访客', v.month || 0, '含 ' + (v.repeat_month || 0) + ' 位回访用户', '2');
    html += statCard('📆', '本年访客', v.year || 0, '总访问 ' + (v.year_total || 0) + ' 次', '3');
    html += statCard('🔄', '历史累计', v.all_time_unique || 0, '累计访问 ' + (v.all_time_total || 0) + ' 次', '4');
    html += '</div>';

    // ── Chart + API status ──
    html += '<div class="admin-grid-2">';

    // Chart panel
    html += '<div class="admin-panel">';
    html += '<div class="admin-panel-header">';
    html += '<span class="admin-panel-title">📈 访客趋势（近30天）</span>';
    html += '</div>';
    html += '<div class="admin-chart-container"><canvas id="visitorChart"></canvas></div>';
    html += '<div class="chart-legend">';
    html += '<span class="chart-legend-item"><span class="chart-legend-dot unique"></span> 独立访客</span>';
    html += '<span class="chart-legend-item"><span class="chart-legend-dot total"></span> 总访问</span>';
    html += '</div>';
    html += '</div>';

    // API status panel
    html += '<div class="admin-panel" id="adminApiSection">';
    html += '<div class="admin-panel-header">';
    html += '<span class="admin-panel-title">🔌 API 状态</span>';
    html += '<div class="admin-api-header-right">';
    html += '<span class="admin-api-header-spinner" id="apiHeaderSpinner" style="display:none"></span>';
    html += '<span class="admin-api-summary warn" id="apiSummary">待检测</span>';
    html += '<select class="admin-interval-select" id="apiIntervalSelect">';
    html +=   '<option value="60000">1分钟</option>';
    html +=   '<option value="300000">5分钟</option>';
    html +=   '<option value="600000">10分钟</option>';
    html +=   '<option value="1800000">半小时</option>';
    html +=   '<option value="3600000">1小时</option>';
    html +=   '<option value="21600000">6小时</option>';
    html +=   '<option value="43200000">12小时</option>';
    html +=   '<option value="86400000" selected>24小时</option>';
    html += '</select>';
    html += '</div>';
    html += '</div>';
    html += '<div class="admin-api-grid" id="apiGrid"></div>';
    html += '</div>';

    html += '</div>'; // end admin-grid-2

    // ── Hourly distribution wave chart ──
    html += '<div class="admin-panel">';
    html += '<div class="admin-panel-header">';
    html += '<span class="admin-panel-title">⏰ 访客时段分布（近30天）</span>';
    html += '<span class="hourly-peak-badge" id="hourlyPeakBadge">—</span>';
    html += '</div>';
    html += '<div class="admin-hourly-chart-container">';
    html += '<canvas id="hourlyWaveChart"></canvas>';
    html += '</div>';
    html += '<div class="hourly-xlabels" id="hourlyXLabels"></div>';
    html += '</div>';

    // ── Rankings ──
    html += '<div class="admin-grid-2">';

    // Top songs
    html += '<div class="admin-panel">';
    html += '<div class="admin-panel-header">';
    html += '<span class="admin-panel-title">🔥 下载最多单曲 TOP5</span>';
    html += '<span style="font-size:11px;color:#595959">成功下载次数排名</span>';
    html += '</div>';
    if (top.length) {
      html += '<ul class="admin-rank-list">';
      top.forEach(function (s, i) {
        html += '<li class="admin-rank-item">';
        html += '<span class="admin-rank-num">' + (i + 1) + '</span>';
        html += '<div class="admin-rank-info">';
        html += '<div class="admin-rank-title">' + escHTML(s.song_title) + '</div>';
        html += '<div class="admin-rank-sub">' + escHTML(s.song_artist) + ' · ' + escHTML(s.platform || '') + '</div>';
        html += '</div>';
        html += '<span class="admin-rank-count">' + s.cnt + ' 次</span>';
        html += '</li>';
      });
      html += '</ul>';
    } else {
      html += '<div class="admin-empty"><span class="admin-empty-icon">📭</span>暂无下载记录</div>';
    }
    html += '</div>';

    // Top channels
    html += '<div class="admin-panel">';
    html += '<div class="admin-panel-header">';
    html += '<span class="admin-panel-title">📡 下载渠道使用量</span>';
    html += '<span style="font-size:11px;color:#595959">成功: ' + (dl.success || 0) + ' / 总计: ' + (dl.total || 0) + '</span>';
    html += '</div>';
    if (ch.length) {
      var maxCnt = ch[0].cnt || 1;
      ch.forEach(function (c, i) {
        var pct = Math.round((c.cnt / maxCnt) * 100);
        var barClass = i === 0 ? 'hot' : (i < 3 ? 'warm' : (i < 5 ? 'cool' : 'neutral'));
        html += '<div class="admin-channel-item">';
        html += '<span class="admin-channel-name">' + escHTML(c.channel || 'unknown') + '</span>';
        html += '<div class="admin-channel-bar-wrap">';
        html += '<div class="admin-channel-bar ' + barClass + '" style="width:' + pct + '%"></div>';
        html += '</div>';
        html += '<span class="admin-channel-count">' + c.cnt + ' (' + (c.success_cnt || 0) + '✓)</span>';
        html += '</div>';
      });
    } else {
      html += '<div class="admin-empty"><span class="admin-empty-icon">📭</span>暂无渠道数据</div>';
    }
    html += '</div>';

    html += '</div>'; // end admin-grid-2

    // ── Recent downloads ──
    html += '<div class="admin-panel">';
    html += '<div class="admin-panel-header">';
    html += '<span class="admin-panel-title">📋 最近下载记录</span>';
    html += '</div>';
    if (recent.length) {
      html += '<div style="overflow-x:auto">';
      html += '<table class="admin-recent-table">';
      html += '<thead><tr>';
      html += '<th>歌曲</th><th>歌手</th><th class="col-platform">平台</th><th>渠道</th>';
      html += '<th>状态</th><th class="col-time">时间</th>';
      html += '</tr></thead><tbody>';
      recent.forEach(function (r) {
        html += '<tr>';
        html += '<td style="max-width:140px;overflow:hidden;text-overflow:ellipsis">' + escHTML(r.song_title || '—') + '</td>';
        html += '<td style="color:#8c8c8c;max-width:100px;overflow:hidden;text-overflow:ellipsis">' + escHTML(r.song_artist || '—') + '</td>';
        html += '<td class="col-platform">' + escHTML(r.platform || '—') + '</td>';
        html += '<td style="font-size:10px;color:#8c8c8c">' + escHTML(r.channel || '—') + '</td>';
        html += '<td><span class="admin-badge ' + (r.success ? 'success' : 'fail') + '">' + (r.success ? '成功' : '失败') + '</span></td>';
        html += '<td class="col-time" style="font-size:10px;color:#595959">' + (r.download_time || '').slice(5, 16).replace('T', ' ') + '</td>';
        html += '</tr>';
      });
      html += '</tbody></table></div>';
    } else {
      html += '<div class="admin-empty"><span class="admin-empty-icon">📭</span>暂无下载记录</div>';
    }
    html += '</div>';

    // ── Footer ──
    html += '<div class="admin-footer">';
    html += '<span class="admin-footer-item">💾 总下载: <strong>' + (dl.total || 0) + '</strong></span>';
    html += '<span class="admin-footer-item">✅ 成功率: <strong>' + (dl.total > 0 ? Math.round((dl.success || 0) / dl.total * 100) : 0) + '%</strong></span>';
    html += '<span class="admin-footer-item">📅 今日下载: <strong>' + (dl.today || 0) + '</strong></span>';
    html += '<span class="admin-footer-item">🕐 数据生成: <strong>' + (d.generated_at || '').slice(11, 19) + '</strong></span>';
    html += '</div>';

    // Set content
    document.getElementById('adminContent').innerHTML = html;

    // Draw chart after DOM update
    setTimeout(function () {
      drawVisitorChart(trend);
      drawHourlyWaveChart(d.hourly || []);
    }, 100);

    // Init API cards and trigger first check after DOM settles
    setTimeout(function () {
      initAPICards();
      checkAPIStatus();
    }, 300);
  }

  // ═════════════════════════════════════════════════════════════════════
  // CHART (Canvas) — with interactive hover tooltips
  // ═════════════════════════════════════════════════════════════════════
  function drawVisitorChart(trend, hoverIdx) {
    var canvas = document.getElementById('visitorChart');
    if (!canvas) return;

    var container = canvas.parentElement;
    var dpr = window.devicePixelRatio || 1;
    var rect = container.getBoundingClientRect();
    var W = rect.width;
    var H = rect.height || 220;

    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';

    var ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);

    // Clear
    ctx.clearRect(0, 0, W, H);

    if (!trend || !trend.length) {
      ctx.fillStyle = '#595959';
      ctx.font = '13px -apple-system, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('暂无访客数据', W / 2, H / 2);
      return;
    }

    var padding = { top: 16, right: 20, bottom: 32, left: 44 };
    var pw = W - padding.left - padding.right;
    var ph = H - padding.top - padding.bottom;

    // Find max value
    var maxVal = 0;
    trend.forEach(function (d) {
      maxVal = Math.max(maxVal, d.total_visits || 0, d.unique_visitors || 0);
    });
    if (maxVal === 0) maxVal = 10;

    // Nice round max
    var niceMax = Math.ceil(maxVal * 1.2);
    if (niceMax < 10) niceMax = 10;

    // Grid lines
    var gridLines = 5;
    ctx.strokeStyle = 'rgba(255,255,255,0.04)';
    ctx.lineWidth = 1;
    for (var i = 0; i <= gridLines; i++) {
      var y = padding.top + (ph / gridLines) * i;
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(W - padding.right, y);
      ctx.stroke();

      // Y-axis labels
      var val = Math.round(niceMax - (niceMax / gridLines) * i);
      ctx.fillStyle = '#595959';
      ctx.font = '10px -apple-system, sans-serif';
      ctx.textAlign = 'right';
      ctx.fillText(val, padding.left - 8, y + 4);
    }

    // X-axis labels (show every ~7 days)
    var step = trend.length <= 10 ? 1 : Math.ceil(trend.length / 8);
    ctx.textAlign = 'center';
    trend.forEach(function (d, i) {
      if (i % step === 0 || i === trend.length - 1) {
        var x = padding.left + (pw / (trend.length - 1)) * i;
        var label = d.date.slice(5); // MM-DD
        ctx.fillStyle = '#595959';
        ctx.font = '9px -apple-system, sans-serif';
        ctx.fillText(label, x, H - padding.bottom + 16);
      }
    });

    // Helper: data → canvas coords
    function xPos(i) { return padding.left + (pw / Math.max(1, trend.length - 1)) * i; }
    function yPos(v) { return padding.top + ph - (v / niceMax) * ph; }

    // Draw total visits area (lighter)
    ctx.beginPath();
    ctx.moveTo(xPos(0), padding.top + ph);
    trend.forEach(function (d, i) {
      ctx.lineTo(xPos(i), yPos(d.total_visits || 0));
    });
    ctx.lineTo(xPos(trend.length - 1), padding.top + ph);
    ctx.closePath();
    var totalGrad = ctx.createLinearGradient(0, padding.top, 0, padding.top + ph);
    totalGrad.addColorStop(0, 'rgba(250,140,22,0.18)');
    totalGrad.addColorStop(1, 'rgba(250,140,22,0.01)');
    ctx.fillStyle = totalGrad;
    ctx.fill();

    // Draw total visits line
    ctx.beginPath();
    trend.forEach(function (d, i) {
      var x = xPos(i), y = yPos(d.total_visits || 0);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = 'rgba(250,140,22,0.35)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 4]);
    ctx.stroke();
    ctx.setLineDash([]);

    // Draw unique visitors area
    ctx.beginPath();
    ctx.moveTo(xPos(0), padding.top + ph);
    trend.forEach(function (d, i) {
      ctx.lineTo(xPos(i), yPos(d.unique_visitors || 0));
    });
    ctx.lineTo(xPos(trend.length - 1), padding.top + ph);
    ctx.closePath();
    var uniqueGrad = ctx.createLinearGradient(0, padding.top, 0, padding.top + ph);
    uniqueGrad.addColorStop(0, 'rgba(250,140,22,0.35)');
    uniqueGrad.addColorStop(1, 'rgba(250,140,22,0.02)');
    ctx.fillStyle = uniqueGrad;
    ctx.fill();

    // Draw unique visitors line
    ctx.beginPath();
    trend.forEach(function (d, i) {
      var x = xPos(i), y = yPos(d.unique_visitors || 0);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = '#fa8c16';
    ctx.lineWidth = 2;
    ctx.stroke();

    // Draw dots on unique line (dimmed when hover is active)
    var hasHover = hoverIdx !== undefined && hoverIdx >= 0 && hoverIdx < trend.length;
    trend.forEach(function (d, i) {
      var x = xPos(i), y = yPos(d.unique_visitors || 0);
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fillStyle = hasHover ? 'rgba(250,140,22,0.35)' : '#fa8c16';
      ctx.fill();
      ctx.beginPath();
      ctx.arc(x, y, 6, 0, Math.PI * 2);
      ctx.fillStyle = hasHover ? 'rgba(250,140,22,0.06)' : 'rgba(250,140,22,0.15)';
      ctx.fill();
    });

    // ── Hover overlay ──
    if (hasHover) {
      var hd = trend[hoverIdx];
      var hx = xPos(hoverIdx);
      var huy = yPos(hd.unique_visitors || 0);
      var hty = yPos(hd.total_visits || 0);

      // 1) Vertical dashed guide line
      ctx.beginPath();
      ctx.moveTo(hx, padding.top);
      ctx.lineTo(hx, padding.top + ph);
      ctx.strokeStyle = 'rgba(250,140,22,0.25)';
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 5]);
      ctx.stroke();
      ctx.setLineDash([]);

      // 2) Intersection dot on unique line — white fill + thin orange border
      ctx.beginPath();
      ctx.arc(hx, huy, 6.5, 0, Math.PI * 2);
      ctx.fillStyle = '#fff';
      ctx.fill();
      ctx.strokeStyle = '#fa8c16';
      ctx.lineWidth = 1.8;
      ctx.stroke();

      // 3) Matching dot on total line (smaller, faint)
      ctx.beginPath();
      ctx.arc(hx, hty, 4, 0, Math.PI * 2);
      ctx.fillStyle = '#fff';
      ctx.fill();
      ctx.strokeStyle = 'rgba(250,140,22,0.45)';
      ctx.lineWidth = 1.2;
      ctx.stroke();

      // 4) Tooltip card
      var tipW = 140, tipH = 52;
      var tipX = hx + 14;
      if (tipX + tipW > W - padding.right) tipX = hx - tipW - 14;
      var tipY = huy - tipH - 14;
      if (tipY < padding.top) tipY = huy + 14;

      // Card background
      ctx.fillStyle = 'rgba(20,20,20,0.94)';
      ctx.strokeStyle = 'rgba(250,140,22,0.3)';
      ctx.lineWidth = 1;
      var br = 8, bx = tipX, by = tipY, bw = tipW, bh = tipH;
      ctx.beginPath();
      ctx.moveTo(bx + br, by);
      ctx.lineTo(bx + bw - br, by);
      ctx.arcTo(bx + bw, by, bx + bw, by + br, br);
      ctx.lineTo(bx + bw, by + bh - br);
      ctx.arcTo(bx + bw, by + bh, bx + bw - br, by + bh, br);
      ctx.lineTo(bx + br, by + bh);
      ctx.arcTo(bx, by + bh, bx, by + bh - br, br);
      ctx.lineTo(bx, by + br);
      ctx.arcTo(bx, by, bx + br, by, br);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();

      // Card text
      var dateLabel = hd.date.slice(5); // MM-DD
      ctx.fillStyle = '#8c8c8c';
      ctx.font = '10px -apple-system, sans-serif';
      ctx.textAlign = 'left';
      ctx.fillText(dateLabel, tipX + 10, tipY + 17);

      ctx.fillStyle = '#fff';
      ctx.font = 'bold 14px -apple-system, sans-serif';
      ctx.fillText((hd.unique_visitors || 0) + ' 人', tipX + 10, tipY + 35);

      ctx.fillStyle = '#fa8c16';
      ctx.font = '10px -apple-system, sans-serif';
      ctx.fillText('总访问 ' + (hd.total_visits || 0) + ' 次', tipX + 10, tipY + 47);
    }

    // Store geometry for mouse interaction
    canvas.__trendData = trend;
    canvas.__trendGeo = { padding: padding, pw: pw, ph: ph, niceMax: niceMax, W: W, H: H };
  }

  // ═════════════════════════════════════════════════════════════════════
  // HOURLY WAVE CHART — smooth bezier wave for 24h distribution
  // ═════════════════════════════════════════════════════════════════════
  function drawHourlyWaveChart(hourly) {
    var canvas = document.getElementById('hourlyWaveChart');
    var badge = document.getElementById('hourlyPeakBadge');
    var xLabels = document.getElementById('hourlyXLabels');
    if (!canvas) return;

    var container = canvas.parentElement;
    var dpr = window.devicePixelRatio || 1;
    var rect = container.getBoundingClientRect();
    var W = rect.width;
    var H = 280;

    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';

    var ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    if (!hourly || !hourly.length) {
      ctx.fillStyle = '#595959';
      ctx.font = '13px -apple-system, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('暂无时段数据', W / 2, H / 2);
      return;
    }

    var padding = { top: 24, right: 28, bottom: 24, left: 48 };
    var pw = W - padding.left - padding.right;
    var ph = H - padding.top - padding.bottom;

    // Find max + peak
    var maxVisits = 0, peakHour = 0, peakVisits = 0;
    hourly.forEach(function (d) {
      if (d.visits > maxVisits) maxVisits = d.visits;
      if (d.visits > peakVisits) { peakVisits = d.visits; peakHour = d.hour; }
    });
    if (maxVisits === 0) maxVisits = 10;
    var niceMax = Math.ceil(maxVisits * 1.3);
    if (niceMax < 10) niceMax = 10;

    // Update peak badge
    if (badge && peakVisits > 0) {
      badge.textContent = '🔺 峰值 ' + peakHour + ':00 · ' + peakVisits + ' 次';
      badge.style.display = 'inline-flex';
    } else if (badge) {
      badge.style.display = 'none';
    }

    // X-axis label positions
    var labelHours = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22];
    if (xLabels) {
      xLabels.innerHTML = labelHours.map(function (h) {
        return '<span>' + (h < 10 ? '0' + h : h) + ':00</span>';
      }).join('');
    }

    // Helper: data → canvas coords
    function xPos(i) { return padding.left + (pw / 23) * i; }
    function yPos(v) { return padding.top + ph - (v / niceMax) * ph; }

    // ── Grid lines ──
    var gridLines = 5;
    ctx.strokeStyle = 'rgba(255,255,255,0.03)';
    ctx.lineWidth = 1;
    for (var gi = 0; gi <= gridLines; gi++) {
      var gy = padding.top + (ph / gridLines) * gi;
      ctx.beginPath();
      ctx.moveTo(padding.left, gy);
      ctx.lineTo(W - padding.right, gy);
      ctx.stroke();

      var gv = Math.round(niceMax - (niceMax / gridLines) * gi);
      ctx.fillStyle = '#595959';
      ctx.font = '10px -apple-system, sans-serif';
      ctx.textAlign = 'right';
      ctx.fillText(gv, padding.left - 8, gy + 4);
    }

    // ── Build smooth curve points using Catmull-Rom → Bezier ──
    var pts = hourly.map(function (d, i) {
      return { x: xPos(i), y: yPos(d.visits) };
    });

    // ── Fill area under wave ──
    ctx.beginPath();
    ctx.moveTo(pts[0].x, padding.top + ph);

    for (var i = 0; i < pts.length - 1; i++) {
      var p0 = i > 0 ? pts[i - 1] : pts[i];
      var p1 = pts[i];
      var p2 = pts[i + 1];
      var p3 = i < pts.length - 2 ? pts[i + 2] : pts[i + 1];

      var cp1x = p1.x + (p2.x - p0.x) / 6;
      var cp1y = p1.y + (p2.y - p0.y) / 6;
      var cp2x = p2.x - (p3.x - p1.x) / 6;
      var cp2y = p2.y - (p3.y - p1.y) / 6;

      ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y);
    }

    ctx.lineTo(pts[pts.length - 1].x, padding.top + ph);
    ctx.closePath();

    // Wave gradient fill
    var waveGrad = ctx.createLinearGradient(0, padding.top, 0, padding.top + ph);
    waveGrad.addColorStop(0, 'rgba(250,140,22,0.28)');
    waveGrad.addColorStop(0.5, 'rgba(250,140,22,0.08)');
    waveGrad.addColorStop(1, 'rgba(250,140,22,0.01)');
    ctx.fillStyle = waveGrad;
    ctx.fill();

    // ── Wave stroke line ──
    ctx.beginPath();
    ctx.moveTo(pts[0].x, pts[0].y);
    for (var j = 0; j < pts.length - 1; j++) {
      var q0 = j > 0 ? pts[j - 1] : pts[j];
      var q1 = pts[j];
      var q2 = pts[j + 1];
      var q3 = j < pts.length - 2 ? pts[j + 2] : pts[j + 1];

      var c1x = q1.x + (q2.x - q0.x) / 6;
      var c1y = q1.y + (q2.y - q0.y) / 6;
      var c2x = q2.x - (q3.x - q1.x) / 6;
      var c2y = q2.y - (q3.y - q1.y) / 6;

      ctx.bezierCurveTo(c1x, c1y, c2x, c2y, q2.x, q2.y);
    }
    ctx.strokeStyle = '#fa8c16';
    ctx.lineWidth = 2.5;
    ctx.shadowColor = 'rgba(250,140,22,0.5)';
    ctx.shadowBlur = 12;
    ctx.stroke();
    ctx.shadowColor = 'transparent';
    ctx.shadowBlur = 0;

    // ── Peak point highlight ──
    var peakPt = pts[peakHour];
    if (peakVisits > 0) {
      // Outer glow ring
      ctx.beginPath();
      ctx.arc(peakPt.x, peakPt.y, 10, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(250,140,22,0.15)';
      ctx.fill();

      ctx.beginPath();
      ctx.arc(peakPt.x, peakPt.y, 6, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(250,140,22,0.25)';
      ctx.fill();

      // Core dot
      ctx.beginPath();
      ctx.arc(peakPt.x, peakPt.y, 4, 0, Math.PI * 2);
      ctx.fillStyle = '#fa8c16';
      ctx.fill();
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 1.5;
      ctx.stroke();

      // Peak label above dot
      var labelY = peakPt.y - 18;
      var labelText = peakVisits + ' 次';
      ctx.font = 'bold 11px -apple-system, sans-serif';
      var textW = ctx.measureText(labelText).width;
      var bubbleW = textW + 14;
      var bubbleH = 20;

      // Ensure label stays within canvas
      var bubbleX = peakPt.x - bubbleW / 2;
      if (bubbleX < padding.left) bubbleX = padding.left;
      if (bubbleX + bubbleW > W - padding.right) bubbleX = W - padding.right - bubbleW;

      ctx.fillStyle = 'rgba(250,140,22,0.9)';
      ctx.beginPath();
      var br = 10, bx = bubbleX, by = labelY - bubbleH / 2, bw = bubbleW, bh = bubbleH;
      ctx.moveTo(bx + br, by);
      ctx.lineTo(bx + bw - br, by);
      ctx.arcTo(bx + bw, by, bx + bw, by + br, br);
      ctx.lineTo(bx + bw, by + bh - br);
      ctx.arcTo(bx + bw, by + bh, bx + bw - br, by + bh, br);
      ctx.lineTo(bx + br, by + bh);
      ctx.arcTo(bx, by + bh, bx, by + bh - br, br);
      ctx.lineTo(bx, by + br);
      ctx.arcTo(bx, by, bx + br, by, br);
      ctx.closePath();
      ctx.fill();

      ctx.fillStyle = '#fff';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(labelText, bubbleX + bubbleW / 2, labelY);
    }

    // Store data for resize + hover
    canvas.__hourlyData = hourly;
    canvas.__hourlyGeo = { padding: padding, pw: pw, ph: ph, niceMax: niceMax, W: W, H: H };
  }

  // ═════════════════════════════════════════════════════════════════════
  // CANVAS MOUSE INTERACTION — hover tooltips on both charts
  // ═════════════════════════════════════════════════════════════════════
  function attachChartHover(canvasId, drawFn, dataKey, geoKey) {
    var canvas = document.getElementById(canvasId);
    if (!canvas) return;
    // Already attached? Skip
    if (canvas.__hoverAttached) return;
    canvas.__hoverAttached = true;

    var hoverIdx = undefined;
    var rafId = null;

    canvas.addEventListener('mousemove', function (e) {
      var geo = canvas[geoKey];
      var data = canvas[dataKey];
      if (!geo || !data || !data.length) return;

      var rect = canvas.getBoundingClientRect();
      var mx = e.clientX - rect.left;

      // Find nearest data point by x-distance
      var bestIdx = 0, bestDist = Infinity;
      for (var i = 0; i < data.length; i++) {
        var px = geo.padding.left + (geo.pw / Math.max(1, data.length - 1)) * i;
        var dist = Math.abs(mx - px);
        if (dist < bestDist) { bestDist = dist; bestIdx = i; }
      }

      // Only trigger within ~30px of a data point
      if (bestDist < 30) {
        if (hoverIdx !== bestIdx) {
          hoverIdx = bestIdx;
          if (rafId) cancelAnimationFrame(rafId);
          rafId = requestAnimationFrame(function () {
            drawFn(data, hoverIdx);
          });
        }
      } else if (hoverIdx !== undefined) {
        hoverIdx = undefined;
        if (rafId) cancelAnimationFrame(rafId);
        rafId = requestAnimationFrame(function () {
          drawFn(data, undefined);
        });
      }
    });

    canvas.addEventListener('mouseleave', function () {
      if (hoverIdx !== undefined) {
        hoverIdx = undefined;
        if (rafId) cancelAnimationFrame(rafId);
        rafId = requestAnimationFrame(function () {
          drawFn(canvas[dataKey], undefined);
        });
      }
    });
  }

  // ── Hover-aware redraw for hourly wave chart ──
  function drawHourlyWaveChartHover(hourly, hoverIdx) {
    var canvas = document.getElementById('hourlyWaveChart');
    var badge = document.getElementById('hourlyPeakBadge');
    var xLabels = document.getElementById('hourlyXLabels');
    if (!canvas) return;

    var container = canvas.parentElement;
    var dpr = window.devicePixelRatio || 1;
    var rect = container.getBoundingClientRect();
    var W = rect.width;
    var H = 280;

    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';

    var ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    if (!hourly || !hourly.length) {
      ctx.fillStyle = '#595959';
      ctx.font = '13px -apple-system, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('暂无时段数据', W / 2, H / 2);
      return;
    }

    var padding = { top: 24, right: 28, bottom: 24, left: 48 };
    var pw = W - padding.left - padding.right;
    var ph = H - padding.top - padding.bottom;

    var maxVisits = 0, peakHour = 0, peakVisits = 0;
    hourly.forEach(function (d) {
      if (d.visits > maxVisits) maxVisits = d.visits;
      if (d.visits > peakVisits) { peakVisits = d.visits; peakHour = d.hour; }
    });
    if (maxVisits === 0) maxVisits = 10;
    var niceMax = Math.ceil(maxVisits * 1.3);
    if (niceMax < 10) niceMax = 10;

    if (badge && peakVisits > 0) {
      badge.textContent = '🔺 峰值 ' + peakHour + ':00 · ' + peakVisits + ' 次';
      badge.style.display = 'inline-flex';
    } else if (badge) {
      badge.style.display = 'none';
    }

    var labelHours = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22];
    if (xLabels) {
      xLabels.innerHTML = labelHours.map(function (h) {
        return '<span>' + (h < 10 ? '0' + h : h) + ':00</span>';
      }).join('');
    }

    function xPos(i) { return padding.left + (pw / 23) * i; }
    function yPos(v) { return padding.top + ph - (v / niceMax) * ph; }

    // Grid lines
    var gridLines = 5;
    ctx.strokeStyle = 'rgba(255,255,255,0.03)';
    ctx.lineWidth = 1;
    for (var gi = 0; gi <= gridLines; gi++) {
      var gy = padding.top + (ph / gridLines) * gi;
      ctx.beginPath();
      ctx.moveTo(padding.left, gy);
      ctx.lineTo(W - padding.right, gy);
      ctx.stroke();
      var gv = Math.round(niceMax - (niceMax / gridLines) * gi);
      ctx.fillStyle = '#595959';
      ctx.font = '10px -apple-system, sans-serif';
      ctx.textAlign = 'right';
      ctx.fillText(gv, padding.left - 8, gy + 4);
    }

    // Build smooth curve points
    var pts = hourly.map(function (d, i) {
      return { x: xPos(i), y: yPos(d.visits) };
    });

    // Fill area under wave
    ctx.beginPath();
    ctx.moveTo(pts[0].x, padding.top + ph);
    for (var i = 0; i < pts.length - 1; i++) {
      var p0 = i > 0 ? pts[i - 1] : pts[i];
      var p1 = pts[i];
      var p2 = pts[i + 1];
      var p3 = i < pts.length - 2 ? pts[i + 2] : pts[i + 1];
      var cp1x = p1.x + (p2.x - p0.x) / 6;
      var cp1y = p1.y + (p2.y - p0.y) / 6;
      var cp2x = p2.x - (p3.x - p1.x) / 6;
      var cp2y = p2.y - (p3.y - p1.y) / 6;
      ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y);
    }
    ctx.lineTo(pts[pts.length - 1].x, padding.top + ph);
    ctx.closePath();

    var waveGrad = ctx.createLinearGradient(0, padding.top, 0, padding.top + ph);
    waveGrad.addColorStop(0, 'rgba(250,140,22,0.28)');
    waveGrad.addColorStop(0.5, 'rgba(250,140,22,0.08)');
    waveGrad.addColorStop(1, 'rgba(250,140,22,0.01)');
    ctx.fillStyle = waveGrad;
    ctx.fill();

    // Wave stroke line
    ctx.beginPath();
    ctx.moveTo(pts[0].x, pts[0].y);
    for (var j = 0; j < pts.length - 1; j++) {
      var q0 = j > 0 ? pts[j - 1] : pts[j];
      var q1 = pts[j];
      var q2 = pts[j + 1];
      var q3 = j < pts.length - 2 ? pts[j + 2] : pts[j + 1];
      var c1x = q1.x + (q2.x - q0.x) / 6;
      var c1y = q1.y + (q2.y - q0.y) / 6;
      var c2x = q2.x - (q3.x - q1.x) / 6;
      var c2y = q2.y - (q3.y - q1.y) / 6;
      ctx.bezierCurveTo(c1x, c1y, c2x, c2y, q2.x, q2.y);
    }
    ctx.strokeStyle = '#fa8c16';
    ctx.lineWidth = 2.5;
    ctx.shadowColor = 'rgba(250,140,22,0.5)';
    ctx.shadowBlur = 12;
    ctx.stroke();
    ctx.shadowColor = 'transparent';
    ctx.shadowBlur = 0;

    // Peak point (rendered dimmed when hover active, hidden if hovered)
    var hasHover = hoverIdx !== undefined && hoverIdx >= 0 && hoverIdx < hourly.length;
    if (peakVisits > 0 && !(hasHover && hoverIdx === peakHour)) {
      var peakPt = pts[peakHour];
      ctx.beginPath(); ctx.arc(peakPt.x, peakPt.y, 10, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(250,140,22,0.15)'; ctx.fill();
      ctx.beginPath(); ctx.arc(peakPt.x, peakPt.y, 6, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(250,140,22,0.25)'; ctx.fill();
      ctx.beginPath(); ctx.arc(peakPt.x, peakPt.y, 4, 0, Math.PI * 2);
      ctx.fillStyle = hasHover ? 'rgba(250,140,22,0.35)' : '#fa8c16'; ctx.fill();
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();

      // Peak label bubble (only when no hover)
      if (!hasHover) {
        var labelY = peakPt.y - 18;
        var labelText = peakVisits + ' 次';
        ctx.font = 'bold 11px -apple-system, sans-serif';
        var textW = ctx.measureText(labelText).width;
        var bubbleW = textW + 14, bubbleH = 20;
        var bubbleX = peakPt.x - bubbleW / 2;
        if (bubbleX < padding.left) bubbleX = padding.left;
        if (bubbleX + bubbleW > W - padding.right) bubbleX = W - padding.right - bubbleW;
        ctx.fillStyle = 'rgba(250,140,22,0.9)';
        ctx.beginPath();
        var br2 = 10, bx2 = bubbleX, by2 = labelY - bubbleH / 2, bw2 = bubbleW, bh2 = bubbleH;
        ctx.moveTo(bx2 + br2, by2); ctx.lineTo(bx2 + bw2 - br2, by2);
        ctx.arcTo(bx2 + bw2, by2, bx2 + bw2, by2 + br2, br2);
        ctx.lineTo(bx2 + bw2, by2 + bh2 - br2);
        ctx.arcTo(bx2 + bw2, by2 + bh2, bx2 + bw2 - br2, by2 + bh2, br2);
        ctx.lineTo(bx2 + br2, by2 + bh2);
        ctx.arcTo(bx2, by2 + bh2, bx2, by2 + bh2 - br2, br2);
        ctx.lineTo(bx2, by2 + br2);
        ctx.arcTo(bx2, by2, bx2 + br2, by2, br2);
        ctx.closePath(); ctx.fill();
        ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(labelText, bubbleX + bubbleW / 2, labelY);
      }
    }

    // ── Hover overlay ──
    if (hasHover) {
      var hd = hourly[hoverIdx];
      var hx = pts[hoverIdx].x;
      var hy = pts[hoverIdx].y;

      // Vertical guide line
      ctx.beginPath();
      ctx.moveTo(hx, padding.top);
      ctx.lineTo(hx, padding.top + ph);
      ctx.strokeStyle = 'rgba(250,140,22,0.25)';
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 5]);
      ctx.stroke();
      ctx.setLineDash([]);

      // Intersection dot — white fill + thin orange border
      ctx.beginPath();
      ctx.arc(hx, hy, 6.5, 0, Math.PI * 2);
      ctx.fillStyle = '#fff';
      ctx.fill();
      ctx.strokeStyle = '#fa8c16';
      ctx.lineWidth = 1.8;
      ctx.stroke();

      // Tooltip card
      var tipW = 108, tipH = 40;
      var tipX = hx + 14;
      if (tipX + tipW > W - padding.right) tipX = hx - tipW - 14;
      var tipY = hy - tipH - 14;
      if (tipY < padding.top) tipY = hy + 14;

      ctx.fillStyle = 'rgba(20,20,20,0.94)';
      ctx.strokeStyle = 'rgba(250,140,22,0.3)';
      ctx.lineWidth = 1;
      var br3 = 8, bx3 = tipX, by3 = tipY, bw3 = tipW, bh3 = tipH;
      ctx.beginPath();
      ctx.moveTo(bx3 + br3, by3); ctx.lineTo(bx3 + bw3 - br3, by3);
      ctx.arcTo(bx3 + bw3, by3, bx3 + bw3, by3 + br3, br3);
      ctx.lineTo(bx3 + bw3, by3 + bh3 - br3);
      ctx.arcTo(bx3 + bw3, by3 + bh3, bx3 + bw3 - br3, by3 + bh3, br3);
      ctx.lineTo(bx3 + br3, by3 + bh3);
      ctx.arcTo(bx3, by3 + bh3, bx3, by3 + bh3 - br3, br3);
      ctx.lineTo(bx3, by3 + br3);
      ctx.arcTo(bx3, by3, bx3 + br3, by3, br3);
      ctx.closePath(); ctx.fill(); ctx.stroke();

      var hourLabel = (hd.hour < 10 ? '0' + hd.hour : hd.hour) + ':00';
      ctx.fillStyle = '#8c8c8c';
      ctx.font = '10px -apple-system, sans-serif';
      ctx.textAlign = 'left';
      ctx.fillText(hourLabel, tipX + 10, tipY + 16);

      ctx.fillStyle = '#fff';
      ctx.font = 'bold 13px -apple-system, sans-serif';
      ctx.fillText((hd.visits || 0) + ' 次访问', tipX + 10, tipY + 32);
    }

    canvas.__hourlyData = hourly;
    canvas.__hourlyGeo = { padding: padding, pw: pw, ph: ph, niceMax: niceMax, W: W, H: H };
  }

  // ── Override drawHourlyWaveChart to use hover-aware version ──
  var _origHourlyDraw = drawHourlyWaveChart;
  drawHourlyWaveChart = function (hourly) {
    // Initial draw without hover
    drawHourlyWaveChartHover(hourly, undefined);
    // Attach hover
    setTimeout(function () {
      attachChartHover('hourlyWaveChart', drawHourlyWaveChartHover, '__hourlyData', '__hourlyGeo');
    }, 150);
  };

  var chartResizeTimer = null;
  window.addEventListener('resize', function () {
    if (!ADMIN_ACTIVE) return;
    clearTimeout(chartResizeTimer);
    chartResizeTimer = setTimeout(function () {
      var vCanvas = document.getElementById('visitorChart');
      if (vCanvas && vCanvas.__trendData) {
        drawVisitorChart(vCanvas.__trendData);
      }
      var hCanvas = document.getElementById('hourlyWaveChart');
      if (hCanvas && hCanvas.__hourlyData) {
        drawHourlyWaveChart(hCanvas.__hourlyData);
      }
    }, 250);
  });


  // ═════════════════════════════════════════════════════════════════════
  // HELPERS
  // ═════════════════════════════════════════════════════════════════════
  function statCard(icon, label, value, sub, nth) {
    return '<div class="admin-stat-card" style="--nth:' + nth + '">' +
      '<span class="admin-stat-icon">' + icon + '</span>' +
      '<div class="admin-stat-label">' + label + '</div>' +
      '<div class="admin-stat-value" data-target="' + value + '">' + fmtNum(value) + '</div>' +
      (sub ? '<div class="admin-stat-sub">' + sub + '</div>' : '') +
    '</div>';
  }

  function fmtNum(n) {
    if (n === undefined || n === null) return '0';
    if (n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, '') + ' 万';
    if (n >= 1000) return n.toLocaleString('zh-CN');
    return String(n);
  }

  // ── Override drawVisitorChart to store trend data + enable hover ──
  var _origDraw = drawVisitorChart;
  drawVisitorChart = function (trend) {
    _origDraw(trend, undefined);
    setTimeout(function () {
      attachChartHover('visitorChart', _origDraw, '__trendData', '__trendGeo');
    }, 150);
  };

  // ═════════════════════════════════════════════════════════════════════
  // BOOT
  // ═════════════════════════════════════════════════════════════════════
  function boot() {
    injectAdminHTML();
    attachLogoTrigger();
    attachPasswordEvents();
    attachDashboardEvents();
  }

  // Run on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
