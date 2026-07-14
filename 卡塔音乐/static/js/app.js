/* 卡塔音乐 — 前端脚本 */
// ============================================================
// State
// ============================================================
let curSort = 'name';
let curPlatform = 'netease';
let curPage = 1, curQuery = '', curSection = 'search', curTotal = 0;
const audio = new Audio();
let currentSong = null;  // {songId, platform, title, artist, cover}

let playlist = [], playIdx = -1;

// ============================================================
// Init
// ============================================================
document.getElementById('searchInput').addEventListener('keydown', function(e) { if (e.key==='Enter') doSearch(1); });
document.getElementById('platformTabs').addEventListener('click', function(e) {
  const b = e.target.closest('.platform-tab');
  if (!b) return;
  document.querySelectorAll('.platform-tab').forEach(function(t) { t.classList.remove('active'); });
  b.classList.add('active');
  curPlatform = b.dataset.platform;
  if (curQuery) doSearch(1);
});

const ncmZ = document.getElementById('ncmZone');
ncmZ.addEventListener('dragover', function(e) { e.preventDefault(); ncmZ.classList.add('drag-over'); });
ncmZ.addEventListener('dragleave', function() { ncmZ.classList.remove('drag-over'); });
ncmZ.addEventListener('drop', function(e) {
  e.preventDefault(); ncmZ.classList.remove('drag-over');
  if (e.dataTransfer.files.length) { document.getElementById('ncmFile').files = e.dataTransfer.files; uploadNcm(); }
});
ncmZ.addEventListener('click', function() { document.getElementById('ncmFile').click(); });
document.getElementById('ncmFile').addEventListener('change', uploadNcm);

audio.addEventListener('timeupdate', function() {
  const pct = audio.duration ? (audio.currentTime/audio.duration)*100 : 0;
  const seek = document.getElementById('playerSeek');
  seek.value = pct;
  seek.style.setProperty('--seek-pct', pct + '%');
  document.getElementById('playerCurTime').textContent = fmtTime(audio.currentTime);
});
audio.addEventListener('loadedmetadata', function() {
  document.getElementById('playerDur').textContent = fmtTime(audio.duration);
});
audio.addEventListener('play', function() { document.getElementById('playerPlayBtn').textContent = '⏸'; });
audio.addEventListener('pause', function() { document.getElementById('playerPlayBtn').textContent = '▶️'; });
audio.addEventListener('ended', function() { playerNext(); });
audio.volume = 0.7;

// Dismissible runtime warning
(function() {
  if (localStorage.getItem('warnDismissed') === '1') {
    var w = document.getElementById('runtimeWarning');
    if (w) w.style.display = 'none';
  }
})();

// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
  switch (e.code) {
    case 'Space':
      e.preventDefault();
      playerToggle();
      break;
    case 'ArrowLeft':
      e.preventDefault();
      audio.currentTime = Math.max(0, audio.currentTime - 5);
      toast('⏪ -5s');
      break;
    case 'ArrowRight':
      e.preventDefault();
      audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 5);
      toast('⏩ +5s');
      break;
    case 'ArrowUp':
      e.preventDefault();
      audio.volume = Math.min(1, audio.volume + 0.1);
      document.getElementById('playerVol').value = audio.volume * 100;
      localStorage.setItem('playerVolume', audio.volume);
      break;
    case 'ArrowDown':
      e.preventDefault();
      audio.volume = Math.max(0, audio.volume - 0.1);
      document.getElementById('playerVol').value = audio.volume * 100;
      localStorage.setItem('playerVolume', audio.volume);
      break;
    case 'Escape':
      e.preventDefault();
      closeAbout(e);
      break;
    case 'Slash':
      if (!e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        document.getElementById('searchInput').focus();
      }
      break;
  }
});

// Volume persistence
(function() {
  var saved = localStorage.getItem('playerVolume');
  if (saved !== null) {
    var v = parseFloat(saved);
    if (!isNaN(v) && v >= 0 && v <= 1) {
      audio.volume = v;
      document.getElementById('playerVol').value = Math.round(v * 100);
    }
  }
})();

// ============================================================
// Helpers
// ============================================================
function toast(m, ms) { ms = ms || 2500; const t=document.getElementById('toast'); t.textContent=m; t.classList.add('show'); setTimeout(function(){t.classList.remove('show');}, ms); }
function esc(s) { const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }

const coverRetryMap = {};
async function retryCover(img, songId, platform) {
  const row = img.closest('.song-row');
  const titleEl = row ? row.querySelector('.title') : null;
  const metaEl = row ? row.querySelector('.meta') : null;
  const title = titleEl ? titleEl.textContent.trim() : '';
  const artist = metaEl ? metaEl.textContent.trim() : '';
  await fetchCoverForSong(songId, platform, title, artist);
  const key = songId + '@' + platform;
  if (coverRetryMap[key] && coverRetryMap[key] !== 'failed') {
    if (img.tagName === 'IMG') {
      img.src = coverRetryMap[key];
    } else {
      var newImg = document.createElement('img');
      newImg.className = 'song-cover';
      newImg.src = coverRetryMap[key];
      newImg.loading = 'lazy';
      img.parentNode.replaceChild(newImg, img);
    }
  } else {
    coverRetryMap[key] = 'failed';
  }
}
function fmtTime(s) { if (!s||isNaN(s)) return '00:00'; const m=Math.floor(s/60), sec=Math.floor(s%60); return m+':'+sec.toString().padStart(2,'0'); }

function switchSort(sort, btn) {
  curSort = sort;
  curSection = 'search';
  document.querySelectorAll('.mode-btn[data-sort]').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
  document.getElementById('searchInput').placeholder = '搜索歌曲、歌手...';
  // Restore search view
  document.getElementById('ncmZone').style.display = 'none';
  document.getElementById('songList').style.display = '';
  document.getElementById('emptyState').style.display = (playlist && playlist.length) ? 'none' : 'block';
  document.getElementById('pagination').style.display = curTotal > 20 ? 'flex' : 'none';
  document.getElementById('resultCount').style.display = '';
  // Re-sort and re-render if we have results
  if (playlist && playlist.length) {
    // Sync any cached covers into playlist before rendering
    for (var i = 0; i < playlist.length; i++) {
      var s = playlist[i];
      if (!s.cover || !s.cover.startsWith('http')) {
        var key = s.id + '@' + s.platform;
        var cached = coverRetryMap[key];
        if (cached && cached !== 'failed') {
          s.cover = cached;
        }
      }
    }
    renderSongs(playlist);
  }
}
function switchSection(s) {
  curSection = s;
  var isNcm = s === 'ncm';
  document.getElementById('ncmZone').style.display = isNcm ? 'block' : 'none';
  document.getElementById('songList').style.display = isNcm ? 'none' : '';
  document.getElementById('emptyState').style.display = isNcm ? 'none' : (playlist && playlist.length ? 'none' : 'block');
  document.getElementById('pagination').style.display = isNcm ? 'none' : (curTotal > 20 ? 'flex' : 'none');
  document.getElementById('resultCount').style.display = isNcm ? 'none' : '';
  if (!isNcm) document.getElementById('searchInput').focus();
}

// ============================================================
// Search
// ============================================================
async function doSearch(page) {
  const q = document.getElementById('searchInput').value.trim();
  if (!q) return;
  curQuery = q; curPage = page; curSection = 'search';
  document.getElementById('loadingArea').style.display = 'block';
  document.getElementById('emptyState').style.display = 'none';
  document.getElementById('songList').innerHTML = '';
  document.getElementById('ncmZone').style.display = 'none';

  try {
    const resp = await fetch('/api/search?q='+encodeURIComponent(q)+'&platform='+curPlatform+'&filter=name&page='+page);
    const data = await resp.json();
    if (data.error) { document.getElementById('songList').innerHTML='<div class="error-box"><h3>搜索失败</h3><p>'+esc(data.error)+'</p></div>'; return; }
    playlist = data.songs;
    renderSongs(data.songs);
    curTotal = data.total || 0;
    document.getElementById('resultCount').textContent = curTotal ? '找到 '+curTotal+' 首' : '';
    renderPagination(page);
  } catch(e) {
    document.getElementById('songList').innerHTML = '<div class="error-box"><h3>搜索失败</h3><p>'+esc(e.message)+'</p></div>';
  } finally {
    document.getElementById('loadingArea').style.display = 'none';
  }
}

function fmtHeat(n) {
  if (!n || n < 1) return '';
  if (n >= 1000000) return (n/1000000).toFixed(1).replace(/\.0$/,'')+'M';
  if (n >= 1000) return (n/1000).toFixed(1).replace(/\.0$/,'')+'k';
  return n.toLocaleString();
}

function proxyCoverUrl(url) {
  if (!url || !url.startsWith('http')) return '';
  // Proxy covers through backend to bypass CDN Referer checks
  return '/api/cover?url=' + encodeURIComponent(url);
}

function renderSongs(songs) {
  const list = document.getElementById('songList');
  if (!songs.length) { list.innerHTML='<div class="empty"><div class="icon">😕</div><p>无结果</p></div>'; return; }

  // Sort by heat if requested
  var sorted = songs.slice();
  if (curSort === 'heat') {
    sorted.sort(function(a, b) { return (b.heat||0) - (a.heat||0); });
  }

  list.innerHTML = sorted.map(function(s,i) {
    var idx = songs.indexOf(s);  // original index for toggleSong
    var coverHtml;
    if (s.cover && s.cover.startsWith('http')) {
      // Try direct CDN first, fall back to proxy on error
      var proxyUrl = proxyCoverUrl(s.cover);
      coverHtml = '<img class="song-cover" src="'+esc(s.cover)+'" loading="lazy" onerror="if(this.src!==\''+esc(proxyUrl)+'\'){this.src=\''+esc(proxyUrl)+'\';}">';
    } else {
      coverHtml = '<div class="song-cover" style="display:flex;align-items:center;justify-content:center;color:#595959;font-size:18px;font-weight:bold;flex-shrink:0">🎵</div>';
    }
    var heatHtml = (s.heat && s.heat > 0) ? '<span class="song-heat">🔥 '+fmtHeat(s.heat)+'</span>' : '';
    return '<li class="song-item" id="song-'+s.id+'">'+
      '<div class="song-row" onclick="toggleSong(\''+s.id+'\',\''+s.platform+'\','+idx+')">'+
        '<span class="song-idx">'+(i+1)+'</span>'+
        coverHtml+
        '<div class="song-info">'+
          '<div class="title">'+esc(s.title)+'</div>'+
          '<div class="meta">'+esc(s.artist)+'</div>'+
        '</div>'+
        heatHtml+
        '<span class="song-platform-tag">'+esc(s.platform_name||s.platform)+'</span>'+
        '<span class="song-arrow">▼</span>'+
      '</div>'+
      '<div class="song-detail" id="detail-'+s.id+'"><div style="text-align:center;padding:12px;color:#595959;">加载中...</div></div>'+
    '</li>';
  }).join('');
  // Auto-fetch missing covers
  songs.forEach(function(s) {
    if (!s.cover || !s.cover.startsWith('http')) {
      fetchCoverForSong(s.id, s.platform, s.title, s.artist);
    }
  });
}

async function fetchCoverForSong(songId, platform, title, artist) {
  const key = songId + '@' + platform;
  if (coverRetryMap[key]) return;
  try {
    let url = '/api/song/'+platform+'/'+songId;
    if (title) url += '?title='+encodeURIComponent(title)+'&artist='+encodeURIComponent(artist||'');
    const resp = await fetch(url);
    const d = await resp.json();
    if (d.cover && d.cover.startsWith('http')) {
      coverRetryMap[key] = d.cover;
      var proxyUrl = proxyCoverUrl(d.cover);
      // Also update playlist entry so re-renders pick up the cover
      for (var pi = 0; pi < playlist.length; pi++) {
        if (playlist[pi].id === songId && playlist[pi].platform === platform) {
          playlist[pi].cover = d.cover;
          break;
        }
      }
      // Update visible DOM elements
      document.querySelectorAll('#song-'+songId+' .song-cover').forEach(function(el) {
        if (el.tagName === 'IMG') {
          el.src = d.cover;
          el.onerror = function() {
            if (this.src !== proxyUrl) this.src = proxyUrl;
          };
        } else {
          var img = document.createElement('img');
          img.className = 'song-cover';
          img.src = d.cover;
          img.loading = 'lazy';
          img.alt = title || '';
          img.onerror = function() {
            if (this.src !== proxyUrl) this.src = proxyUrl;
          };
          el.parentNode.replaceChild(img, el);
        }
      });
    }
  } catch(e) {}
}

async function toggleSong(songId, platform, idx) {
  const item = document.getElementById('song-'+songId);
  const detail = document.getElementById('detail-'+songId);
  if (item.classList.contains('expanded')) { item.classList.remove('expanded'); return; }
  document.querySelectorAll('.song-item.expanded').forEach(function(e){e.classList.remove('expanded');});
  item.classList.add('expanded');

  // Stop any currently playing audio and reset state
  audio.pause();
  audio.src = '';
  document.getElementById('playerPlayBtn').textContent = '▶️';

  // Show player bar and update info
  document.getElementById('playerBar').classList.add('visible');
  playIdx = idx;
  var song = playlist[idx];
  if (song) {
    currentSong = {songId: songId, platform: platform, title: song.title, artist: song.artist, cover: song.cover||'', duration: song.duration||0};
    document.getElementById('playerTitle').textContent = song.title || 'Unknown';
    document.getElementById('playerArtist').textContent = song.artist || 'Unknown';
    // Show duration from search result
    var durMs = song.duration || 0;
    document.getElementById('playerDur').textContent = fmtTime(durMs / 1000);
    document.getElementById('playerCurTime').textContent = '00:00';
    document.getElementById('playerSeek').value = 0;
    document.getElementById('playerSeek').style.setProperty('--seek-pct', '0%');
    if (song.cover && song.cover.startsWith('http')) {
      var playerCover = document.getElementById('playerCover');
      var proxyUrl = proxyCoverUrl(song.cover);
      playerCover.src = song.cover;
      playerCover.onerror = function() {
        if (this.src !== proxyUrl) this.src = proxyUrl;
      };
    } else {
      loadPlayerCover(songId, platform);
    }
  }

  if (detail.dataset.loaded!=='1') {
    try {
      const song = playlist[idx];
      const lrc = song.lyric || '';
      const tEnc = encodeURIComponent(song.title||'');
      const aEnc = encodeURIComponent(song.artist||'');
      detail.innerHTML =
        '<div class="detail-actions">'+
          '<button class="btn btn-primary dl-btn" data-sid="'+songId+'" data-plat="'+platform+'">⬇ 下载 MP3</button>'+
          '<button class="btn btn-outline btn-sm lrc-btn" data-sid="'+songId+'" data-plat="'+platform+'">📝 下载LRC歌词</button>'+
          '<select class="lrc-lang-select">'+
            '<option value="">不翻译</option>'+
            '<option value="zh">翻译为中文</option>'+
            '<option value="en">翻译为英文</option>'+
          '</select>'+
          (lrc ? '<button class="btn btn-outline btn-sm" onclick="toggleLyrics(\''+songId+'\')">👁 预览歌词</button>' : '')+
        '</div>'+
        '<div class="lrc-progress" id="lrc-progress-'+songId+'"><div class="bar"></div><div class="label">翻译中...</div></div>'+
        '<div class="detail-url">ID: '+songId+' | <a href="'+(song.link||'#')+'" target="_blank">源页面 ↗</a></div>'+
        (lrc ? '<div class="lyrics-panel" id="lyrics-'+songId+'">'+esc(lrc).replace(/\n/g,'<br>')+'</div>' : '');
      detail.dataset.loaded = '1';

      // Attach event listeners
      detail.querySelector('.dl-btn').addEventListener('click', function() {
        downloadSong(songId, platform, song.title, song.artist);
      });
      detail.querySelector('.lrc-btn').addEventListener('click', function() {
        downloadLrc(songId, platform, song.title, song.artist);
      });
    } catch(e) {
      detail.innerHTML = '<div class="error-box">加载失败: '+esc(e.message)+'</div>';
    }
  }
}

function toggleLyrics(songId) { document.getElementById('lyrics-'+songId).classList.toggle('show'); }

async function downloadLrc(songId, platform, title, artist) {
  // Find the associated language selector
  var detailEl = document.getElementById('detail-'+songId);
  var sel = detailEl ? detailEl.querySelector('.lrc-lang-select') : null;
  var translateLang = sel ? sel.value : '';

  var url = '/api/lrc/'+platform+'/'+songId;
  if (title && title !== 'Unknown') url += '?title='+encodeURIComponent(title)+'&artist='+encodeURIComponent(artist||'');
  if (translateLang) url += '&translate='+encodeURIComponent(translateLang);

  if (translateLang) {
    // Translation route: use fetch so we can show progress
    var progressEl = document.getElementById('lrc-progress-'+songId);
    if (progressEl) {
      progressEl.classList.add('active');
      progressEl.querySelector('.label').textContent = '翻译中...';
    }

    try {
      var resp = await fetch(url);
      if (!resp.ok) {
        var errData = null;
        try { errData = await resp.json(); } catch(e) {}
        throw new Error((errData && errData.error) || '翻译失败 (HTTP '+resp.status+')');
      }
      var blob = await resp.blob();

      if (progressEl) {
        progressEl.querySelector('.label').textContent = '翻译完成，开始下载...';
        await new Promise(function(r) { setTimeout(r, 400); });
      }

      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      var disp = resp.headers.get('Content-Disposition')||'';
      var fname = null;
      var starMatch = disp.match(/filename\*=UTF-8''([^;]*)/);
      if (starMatch) {
        try { fname = decodeURIComponent(starMatch[1]); } catch(e) { fname = null; }
      }
      if (!fname) {
        var plainMatch = disp.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/);
        fname = plainMatch ? plainMatch[1].replace(/['"]/g,'') : 'song.lrc';
      }
      a.download = fname;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      URL.revokeObjectURL(a.href);

      var langLabel = translateLang==='zh' ? '中文' : '英文';
      toast('✅ 翻译完成 ('+langLabel+')，LRC 已下载');
    } catch(e) {
      toast('❌ 翻译失败: '+e.message);
    } finally {
      if (progressEl) progressEl.classList.remove('active');
    }
  } else {
    // No translation: original direct download behavior
    var a = document.createElement('a');
    a.href = url;
    a.download = '';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    toast('📝 LRC 歌词下载已开始');
  }
}

// ============================================================
// Player
// ============================================================
async function playSong(songId, platform, title, artist, cover) {
  toast('正在获取音源...');
  try {
    const resp = await fetch('/api/p/'+songId+'?platform='+platform+'&id='+songId);
    const data = await resp.json();
    if (data.success && data.url) {
      audio.src = data.url;
      audio.play().catch(function(e){toast('播放失败: '+e.message)});
      document.getElementById('playerTitle').textContent = title;
      document.getElementById('playerArtist').textContent = artist;
      if (cover && cover.startsWith('http')) {
        var playerCover = document.getElementById('playerCover');
        var proxyUrl = proxyCoverUrl(cover);
        playerCover.src = cover;
        playerCover.onerror = function() {
          if (this.src !== proxyUrl) this.src = proxyUrl;
        };
      } else {
        loadPlayerCover(songId, platform);
      }
      toast('▶ 正在播放: ' + title);
    } else {
      toast('⚠ 该歌曲暂无可用音源');
    }
  } catch(e) {
    toast('❌ 获取音源失败: ' + e.message);
  }
}

async function loadPlayerCover(songId, platform) {
  try {
    const resp = await fetch('/api/song/'+platform+'/'+songId);
    const d = await resp.json();
    if (d.cover && d.cover.startsWith('http')) {
      var playerCover = document.getElementById('playerCover');
      var proxyUrl = proxyCoverUrl(d.cover);
      playerCover.src = d.cover;
      playerCover.onerror = function() {
        if (this.src !== proxyUrl) this.src = proxyUrl;
      };
    }
  } catch(e) {}
}

async function playerToggle() {
  // If no song source loaded yet, load the current song first
  if (!audio.src || audio.src === window.location.href) {
    if (!currentSong) { toast('请先选择一首歌曲'); return; }
    await playCurrentSong();
    return;
  }
  if (audio.paused) {
    // Apply seek bar position before playing
    var seek = document.getElementById('playerSeek');
    if (audio.duration && seek.value > 0) {
      audio.currentTime = (seek.value / 100) * audio.duration;
    }
    audio.play().catch(function(e){toast('播放失败: '+e.message)});
  } else {
    audio.pause();
  }
}

async function playCurrentSong() {
  if (!currentSong) return;
  var cs = currentSong;
  toast('正在获取音源...');
  try {
    var resp = await fetch('/api/p/'+cs.songId+'?platform='+cs.platform+'&id='+cs.songId);
    var data = await resp.json();
    if (data.success && data.url) {
      audio.src = data.url;
      // Respect seek bar position
      var seek = document.getElementById('playerSeek');
      var seekPct = parseFloat(seek.value) || 0;
      if (seekPct > 0) {
        await new Promise(function(resolve) {
          var timeout = setTimeout(resolve, 4000);
          var onMeta = function() {
            clearTimeout(timeout);
            audio.removeEventListener('loadedmetadata', onMeta);
            audio.currentTime = (seekPct / 100) * audio.duration;
            resolve();
          };
          audio.addEventListener('loadedmetadata', onMeta);
        });
      }
      audio.play().catch(function(e){toast('播放失败: '+e.message)});
      document.getElementById('playerTitle').textContent = cs.title;
      document.getElementById('playerArtist').textContent = cs.artist;
      toast('▶ 正在播放: ' + cs.title);
    } else {
      toast('⚠ 暂无可用音源，请再次点击播放按钮重试');
    }
  } catch(e) {
    toast('❌ 获取失败，请再次点击播放按钮重试');
  }
}

function playerSeekTo(v) {
  document.getElementById('playerSeek').style.setProperty('--seek-pct', v+'%');
  if (audio.duration) {
    audio.currentTime = (v/100)*audio.duration;
    document.getElementById('playerCurTime').textContent = fmtTime(audio.currentTime);
  } else {
    // Show seek preview even before audio is loaded
    var dur = currentSong && currentSong.duration ? currentSong.duration / 1000 : 0;
    if (dur) document.getElementById('playerCurTime').textContent = fmtTime((v/100)*dur);
  }
}
function playerSetVol(v) { audio.volume = v/100; }
async function playerPrev() {
  // Find current song index in displayed playlist (default to playIdx)
  var idx = -1;
  if (currentSong) {
    for (var i = 0; i < playlist.length; i++) {
      if (playlist[i].id === currentSong.songId && playlist[i].platform === currentSong.platform) {
        idx = i; break;
      }
    }
  }
  if (idx < 0 && playIdx >= 0) idx = playIdx;
  if (idx <= 0 || !playlist.length) { toast('已是第一首'); return; }
  var prev = playlist[idx - 1];
  await toggleSong(prev.id, prev.platform, idx - 1);
  // Scroll target into view
  var el = document.getElementById('song-'+prev.id);
  if (el) el.scrollIntoView({behavior:'smooth',block:'center'});
  // Auto-play the switched song
  playerToggle();
}
async function playerNext() {
  var idx = -1;
  if (currentSong) {
    for (var i = 0; i < playlist.length; i++) {
      if (playlist[i].id === currentSong.songId && playlist[i].platform === currentSong.platform) {
        idx = i; break;
      }
    }
  }
  if (idx < 0 && playIdx >= 0) idx = playIdx;
  if (idx < 0 || idx >= playlist.length - 1 || !playlist.length) { toast('已是最后一首'); return; }
  var next = playlist[idx + 1];
  await toggleSong(next.id, next.platform, idx + 1);
  var el = document.getElementById('song-'+next.id);
  if (el) el.scrollIntoView({behavior:'smooth',block:'center'});
  // Auto-play the switched song
  playerToggle();
}

// ============================================================
// Download
// ============================================================
async function downloadSong(songId, platform, title, artist) {
  toast('正在获取下载链接，请耐心等待...');
  try {
    let url = '/api/download/'+platform+'/'+songId;
    if (title && title !== 'Unknown') url += '?title='+encodeURIComponent(title)+'&artist='+encodeURIComponent(artist||'');
    const resp = await fetch(url);
    const contentType = resp.headers.get('Content-Type') || '';

    if (resp.ok && (contentType.includes('audio') || contentType.includes('octet-stream'))) {
      const blob = await resp.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      const disp = resp.headers.get('Content-Disposition')||'';
      // RFC 5987: filename*=UTF-8''percent-encoded — prefer over ASCII filename=
      var fname = null;
      var starMatch = disp.match(/filename\*=UTF-8''([^;]*)/);
      if (starMatch) {
        try { fname = decodeURIComponent(starMatch[1]); } catch(e) { fname = null; }
      }
      if (!fname) {
        var plainMatch = disp.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/);
        fname = plainMatch ? plainMatch[1].replace(/['"]/g,'') : 'song.mp3';
      }
      a.download = fname;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
      toast('✅ 下载成功！');
    } else if (!resp.ok) {
      const err = await resp.json();
      toast('❌ '+((err||{}).error||'下载失败')+': '+(err.detail||''));
    } else {
      toast('⚠ 未知响应格式');
    }
  } catch(e) { toast('❌ 下载失败: '+e.message); }
}

// ============================================================
// NCM Upload
// ============================================================
async function uploadNcm() {
  const files = document.getElementById('ncmFile').files;
  if (!files.length) return;
  for (const file of files) {
    const fd = new FormData(); fd.append('file', file);
    try {
      const resp = await fetch('/api/ncm/decrypt', {method:'POST', body:fd});
      if (resp.ok) {
        const blob = await resp.blob(), a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = file.name.replace('.ncm','.mp3');
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        URL.revokeObjectURL(a.href);
        toast('✅ '+file.name+' 解密成功');
      } else { const e = await resp.json(); toast('❌ '+file.name+': '+e.error); }
    } catch(e) { toast('❌ '+file.name+': '+e.message); }
  }
  document.getElementById('ncmFile').value = '';
}

function renderPagination(page) {
  const pg = document.getElementById('pagination');
  const perPage = 20;
  const totalPages = Math.max(1, Math.ceil(curTotal / perPage));
  if (totalPages <= 1) { pg.style.display = 'none'; return; }

  let html = '<button '+(page<=1?'disabled':'')+' onclick="doSearch('+(page-1)+')">‹ 上一页</button>';
  const pagesToShow = [];
  if (totalPages <= 8) {
    for (let i=1; i<=totalPages; i++) pagesToShow.push(i);
  } else {
    pagesToShow.push(1,2,3);
    if (page > 4) pagesToShow.push('...');
    for (let i=Math.max(4, page-1); i<=Math.min(totalPages-3, page+1); i++) pagesToShow.push(i);
    if (page < totalPages-3) pagesToShow.push('...');
    pagesToShow.push(totalPages-1, totalPages);
  }

  for (const p of pagesToShow) {
    if (p === '...') {
      html += '<span style="padding:6px 4px;color:#595959">…</span>';
    } else {
      html += '<button class="'+(p===page?'active':'')+'" onclick="doSearch('+p+')">'+p+'</button>';
    }
  }

  html += '<input type="number" id="jumpPage" min="1" max="'+totalPages+'" value="'+page+'" style="width:50px;text-align:center;background:#1f1f1f;color:#d9d9d9;border:1px solid #333;border-radius:16px;padding:6px 8px;font-size:12px" onkeydown="if(event.key===\'Enter\'){const v=parseInt(this.value); if(v>=1)doSearch(v)}">'+
    '<button onclick="var v=parseInt(document.getElementById(\'jumpPage\').value); if(v>=1)doSearch(v)" style="padding:6px 12px;background:#fa8c16;color:#fff;border:none;border-radius:16px;cursor:pointer;font-size:12px">跳转</button>';

  html += '<button '+(page>=totalPages?'disabled':'')+' onclick="doSearch('+(page+1)+')">下一页 ›</button>';
  pg.innerHTML = html;
  pg.style.display = 'flex';
}
function openAbout() {
  document.getElementById('aboutOverlay').classList.add('show');
}
function closeAbout(e) {
  if (e && e.target !== e.currentTarget) return;
  document.getElementById('aboutOverlay').classList.remove('show');
}
