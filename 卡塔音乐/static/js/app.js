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

// Translation cache: preview → download flow
// Key: "songId@platform@lang" → {text, timer}
var lrcCache = {};
var lrcCacheKeys = [];  // ordered by insertion (oldest first), max 3

function cacheLrc(songId, platform, lang, text) {
  var key = songId + '@' + platform + '@' + lang;
  // Clear previous cache for same key if exists
  if (lrcCache[key]) {
    clearTimeout(lrcCache[key].timer);
    var idx = lrcCacheKeys.indexOf(key);
    if (idx >= 0) lrcCacheKeys.splice(idx, 1);
  }
  // Enforce max 3 entries (FIFO eviction)
  while (lrcCacheKeys.length >= 3) {
    var oldest = lrcCacheKeys.shift();
    clearTimeout(lrcCache[oldest].timer);
    delete lrcCache[oldest];
  }
  // Auto-expire after 30 seconds
  var timer = setTimeout(function() {
    var i = lrcCacheKeys.indexOf(key);
    if (i >= 0) lrcCacheKeys.splice(i, 1);
    delete lrcCache[key];
  }, 30000);
  lrcCache[key] = { text: text, timer: timer };
  lrcCacheKeys.push(key);
}

function getCachedLrc(songId, platform, lang) {
  var key = songId + '@' + platform + '@' + lang;
  var entry = lrcCache[key];
  if (!entry) return null;
  // Consume cache — clear timer and remove entry
  clearTimeout(entry.timer);
  var i = lrcCacheKeys.indexOf(key);
  if (i >= 0) lrcCacheKeys.splice(i, 1);
  delete lrcCache[key];
  return entry.text;
}

function clearSongLrcCache(songId, platform) {
  var prefix = songId + '@' + platform + '@';
  for (var k in lrcCache) {
    if (lrcCache.hasOwnProperty(k) && k.indexOf(prefix) === 0) {
      clearTimeout(lrcCache[k].timer);
      var i = lrcCacheKeys.indexOf(k);
      if (i >= 0) lrcCacheKeys.splice(i, 1);
      delete lrcCache[k];
    }
  }
}

// Global raw lyrics cache — preloaded when a song plays, shared with preview/download
// Key: "songId@platform" → {lines:[{time,text}], loaded:bool, raw:string}
var lyricsDataCache = {};

function getLyricsCacheKey(songId, platform) {
  return songId + '@' + platform;
}

function parseLrcToLines(lrcText) {
  if (!lrcText) return [];
  var lines = lrcText.split('\n');
  var result = [];
  for (var i = 0; i < lines.length; i++) {
    var match = lines[i].match(/^\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)/);
    if (match) {
      var mins = parseInt(match[1], 10);
      var secs = parseInt(match[2], 10);
      var ms = parseInt(match[3], 10);
      if (match[3].length === 2) ms *= 10;
      var time = mins * 60 + secs + ms / 1000;
      var text = match[4].trim();
      if (text) result.push({ time: time, text: text });
    }
  }
  return result;
}

async function ensureLyricsLoaded(songId, platform) {
  var key = getLyricsCacheKey(songId, platform);
  if (lyricsDataCache[key] && lyricsDataCache[key].loaded) {
    return lyricsDataCache[key];
  }

  // Mark as loading to prevent duplicate requests
  if (!lyricsDataCache[key]) {
    lyricsDataCache[key] = { lines: [], loaded: false, raw: '' };
  }

  try {
    var resp = await fetch('/api/lyrics/' + platform + '/' + songId);
    var data = await resp.json();
    var lrcText = (data && data.lyric) ? data.lyric : '';
    var lines = parseLrcToLines(lrcText);
    lyricsDataCache[key] = { lines: lines, loaded: true, raw: lrcText };
    return lyricsDataCache[key];
  } catch(e) {
    lyricsDataCache[key] = { lines: [], loaded: true, raw: '' };
    return lyricsDataCache[key];
  }
}

function updatePlayerLyrics(currentTime) {
  var elCur = document.getElementById('playerLyricsCurrent');
  var elNext = document.getElementById('playerLyricsNext');
  if (!elCur || !elNext) return;
  if (!currentSong) { elCur.textContent = ''; elNext.textContent = ''; return; }

  var key = getLyricsCacheKey(currentSong.songId, currentSong.platform);
  var cache = lyricsDataCache[key];
  if (!cache || !cache.loaded || !cache.lines.length) {
    elCur.textContent = ''; elNext.textContent = ''; return;
  }

  var lines = cache.lines;
  var curIdx = -1;
  for (var i = lines.length - 1; i >= 0; i--) {
    if (currentTime >= lines[i].time) { curIdx = i; break; }
  }

  var curText = curIdx >= 0 ? lines[curIdx].text : '';
  var nextText = (curIdx >= 0 && curIdx + 1 < lines.length) ? lines[curIdx + 1].text : '';

  // ── Current line: two-step non-linear scroll ──
  if (elCur.dataset.text !== curText) {
    // Step 1 — exit: slide up + fade out (fast)
    elCur.style.opacity = '0';
    elCur.style.transform = 'translateY(-14px)';
    elCur.style.transitionDuration = '0.14s';

    setTimeout(function() {
      // Step 2 — entry: slide from below + overshoot settle
      elCur.dataset.text = curText;
      elCur.textContent = curText;
      elCur.style.transform = 'translateY(10px)';
      elCur.style.transitionDuration = '0.28s';
      requestAnimationFrame(function() {
        requestAnimationFrame(function() {
          elCur.style.opacity = '1';
          elCur.style.transform = 'translateY(0)';
        });
      });
    }, 150);
  }

  // ── Next line: dimmer, staggered behind current ──
  if (elNext.dataset.text !== nextText) {
    setTimeout(function() {
      elNext.dataset.text = nextText;
      elNext.style.opacity = '0';
      elNext.style.transform = 'translateY(8px)';
      elNext.style.transitionDuration = '0.12s';
      elNext.textContent = nextText;
      requestAnimationFrame(function() {
        requestAnimationFrame(function() {
          elNext.style.opacity = '';   // back to CSS default (0.45)
          elNext.style.transform = 'translateY(0)';
          elNext.style.transitionDuration = '0.28s';
        });
      });
    }, 40);  // slight stagger after current line starts
  }
}

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
  // Update scrolling lyrics in player bar
  updatePlayerLyrics(audio.currentTime);
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
      closeLyricsModal();
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
  document.querySelectorAll('.search-bar .mode-btn').forEach(function(b) { b.classList.remove('active'); });
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
  document.querySelectorAll('.search-bar .mode-btn').forEach(function(b) { b.classList.remove('active'); });
  if (isNcm) {
    var ncmBtn = document.querySelector('.search-bar .mode-btn:not([data-sort])');
    if (ncmBtn) ncmBtn.classList.add('active');
  } else {
    var activeSortBtn = document.querySelector('.search-bar .mode-btn[data-sort="'+curSort+'"]');
    if (activeSortBtn) activeSortBtn.classList.add('active');
  }
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
  document.getElementById('ncmZone').style.display = 'none';

  var skeletonHtml = '';
  for (var i = 0; i < 5; i++) {
    skeletonHtml += '<li class="song-item"><div class="skeleton-row">' +
      '<div class="skeleton-cover"></div>' +
      '<div style="flex:1">' +
        '<div class="skeleton-line medium" style="margin-bottom:6px"></div>' +
        '<div class="skeleton-line short"></div>' +
      '</div></div></li>';
  }
  document.getElementById('songList').innerHTML = skeletonHtml;
  document.getElementById('songList').classList.add('skeleton-list');

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
    document.getElementById('songList').classList.remove('skeleton-list');
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

async function toggleSong(songId, platform, idx, shouldAutoPlay) {
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
  var container = document.querySelector('.container');
  if (container) container.classList.add('player-visible');
  playIdx = idx;
  var song = playlist[idx];
  if (song) {
    currentSong = {songId: songId, platform: platform, title: song.title, artist: song.artist, cover: song.cover||'', duration: song.duration||0};
    document.getElementById('playerTitle').textContent = song.title || 'Unknown';
    document.getElementById('playerArtist').textContent = song.artist || 'Unknown';
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

  updatePlayingIndicator(songId);

  // Preload lyrics in background for instant preview/download
  ensureLyricsLoaded(songId, platform);

  if (detail.dataset.loaded!=='1') {
    try {
      const song = playlist[idx];
      const lrc = song.lyric || '';
      detail.innerHTML = '<div>'+
        '<div class="detail-actions">'+
          '<button class="btn btn-primary dl-btn" data-sid="'+songId+'" data-plat="'+platform+'">⬇ 下载 MP3</button>'+
          '<button class="btn btn-outline btn-sm lrc-btn" data-sid="'+songId+'" data-plat="'+platform+'">📝 下载LRC歌词</button>'+
          '<select class="lrc-lang-select">'+
            '<option value="">不翻译</option>'+
            '<option value="zh">翻译为中文</option>'+
            '<option value="en">翻译为英文</option>'+
          '</select>'+
          '<button class="btn btn-outline btn-sm" onclick="fetchAndPreviewLyrics(\''+songId+'\',\''+platform+'\')">👁 预览歌词</button>'+
        '</div>'+
        '<div class="lrc-progress" id="lrc-progress-'+songId+'"><div class="bar"></div><div class="label">翻译中...</div></div>'+
        '<div class="detail-url">ID: '+songId+' | <a href="'+(song.link||'#')+'" target="_blank">源页面 ↗</a></div>'+
        '<div class="lyrics-panel" id="lyrics-'+songId+'"'+(lrc?' data-fetched="1"':'')+'>'+(lrc?esc(lrc).replace(/\n/g,'<br>'):'')+'</div>'+
        '</div>';
      detail.dataset.loaded = '1';

      detail.querySelector('.dl-btn').addEventListener('click', function() {
        downloadSong(songId, platform, song.title, song.artist);
      });
      detail.querySelector('.lrc-btn').addEventListener('click', function() {
        downloadLrc(songId, platform, song.title, song.artist);
      });
    } catch(e) {
      detail.innerHTML = '<div><div class="error-box">加载失败: '+esc(e.message)+'</div></div>';
    }
  }

  if (shouldAutoPlay !== false) {
    playCurrentSong(true);
    var songEl = document.getElementById('song-'+songId);
    if (songEl) songEl.scrollIntoView({behavior:'smooth',block:'center'});
  }
}

function toggleLyrics(songId) { document.getElementById('lyrics-'+songId).classList.toggle('show'); }

async function fetchAndPreviewLyrics(songId, platform) {
  var modal = document.getElementById('lyricsModal');
  var titleEl = document.getElementById('lyricsModalTitle');
  var bodyEl = document.getElementById('lyricsModalBody');

  // Find song info from playlist
  var songTitle = '', songArtist = '';
  for (var i = 0; i < playlist.length; i++) {
    if (playlist[i].id === songId && playlist[i].platform === platform) {
      songTitle = playlist[i].title || '';
      songArtist = playlist[i].artist || '';
      break;
    }
  }

  // Check if translation language is selected in the detail dropdown
  var detailEl = document.getElementById('detail-'+songId);
  var sel = detailEl ? detailEl.querySelector('.lrc-lang-select') : null;
  var translateLang = sel ? sel.value : '';

  // Show modal with loading state
  var loadingMsg = translateLang ? '正在翻译歌词...' : '加载歌词中...';
  titleEl.textContent = (songTitle ? songTitle + ' - ' + songArtist : '歌词预览');
  bodyEl.innerHTML = '<div style="text-align:center;padding:40px 20px;color:#8c8c8c;"><div class="spinner"></div><p>'+loadingMsg+'</p></div>';
  modal.classList.add('show');

  try {
    var lrcText = '';

    if (translateLang) {
      // Build translation URL (same pattern as downloadLrc)
      var url = '/api/lrc/'+platform+'/'+songId;
      if (songTitle && songTitle !== 'Unknown') {
        url += '?title='+encodeURIComponent(songTitle)+'&artist='+encodeURIComponent(songArtist||'');
      }
      url += (url.indexOf('?')>=0 ? '&' : '?') + 'translate='+encodeURIComponent(translateLang);

      // Show translation progress bar
      bodyEl.innerHTML = '<div style="text-align:center;padding:40px 20px;color:#8c8c8c;">'+
        '<div class="spinner"></div>'+
        '<p>正在翻译歌词，请耐心等待...</p>'+
        '<div class="lrc-progress active" style="max-width:240px;margin:12px auto 0"><div class="bar"></div><div class="label" style="display:block">翻译中...</div></div>'+
        '</div>';

      var resp = await fetch(url);
      if (!resp.ok) {
        var errData = null;
        try { errData = await resp.json(); } catch(e) {}
        throw new Error((errData && errData.error) || '翻译失败 (HTTP '+resp.status+')');
      }
      var blob = await resp.blob();
      lrcText = await blob.text();

      // Cache for potential download
      cacheLrc(songId, platform, translateLang, lrcText);

      // Update title to show translation language
      var langLabel = translateLang==='zh' ? '中文' : '英文';
      titleEl.textContent = (songTitle ? songTitle + ' - ' + songArtist : '歌词预览') + ' [' + langLabel + ']';
    } else {
      // No translation — use global lyrics cache if available
      var cacheEntry = await ensureLyricsLoaded(songId, platform);
      lrcText = cacheEntry.raw || '暂无歌词';
    }

    bodyEl.innerHTML = esc(lrcText).replace(/\n/g,'<br>');
  } catch(e) {
    bodyEl.innerHTML = '<div style="text-align:center;padding:40px 20px;color:#ffa39e;">加载失败: '+esc(e.message)+'</div>';
  }
}

function closeLyricsModal(e) {
  if (e && e.target !== e.currentTarget) return;
  document.getElementById('lyricsModal').classList.remove('show');
}

async function downloadLrc(songId, platform, title, artist) {
  // Find the associated language selector
  var detailEl = document.getElementById('detail-'+songId);
  var sel = detailEl ? detailEl.querySelector('.lrc-lang-select') : null;
  var translateLang = sel ? sel.value : '';

  var url = '/api/lrc/'+platform+'/'+songId;
  if (title && title !== 'Unknown') url += '?title='+encodeURIComponent(title)+'&artist='+encodeURIComponent(artist||'');
  if (translateLang) url += '&translate='+encodeURIComponent(translateLang);

  if (translateLang) {
    var progressEl = document.getElementById('lrc-progress-'+songId);
    var langLabel = translateLang==='zh' ? '中文' : '英文';

    // Check if translation is already cached from preview
    var cached = getCachedLrc(songId, platform, translateLang);
    if (cached) {
      // Use cached translation — skip server round-trip
      if (progressEl) {
        progressEl.classList.add('active');
        progressEl.querySelector('.label').textContent = '使用缓存，开始下载...';
      }
      var cBlob = new Blob([cached], { type: 'text/plain;charset=utf-8' });
      var ca = document.createElement('a');
      ca.href = URL.createObjectURL(cBlob);
      ca.download = (title && title !== 'Unknown' ? title : 'song') + '.lrc';
      document.body.appendChild(ca); ca.click(); document.body.removeChild(ca);
      URL.revokeObjectURL(ca.href);
      if (progressEl) { progressEl.classList.remove('active'); }
      toast('✅ 翻译完成 ('+langLabel+')，LRC 已下载（使用缓存）');
      return;
    }

    // Cache miss — clear any stale cache for this song (different lang) and fetch fresh
    clearSongLrcCache(songId, platform);

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

      toast('✅ 翻译完成 ('+langLabel+')，LRC 已下载');
    } catch(e) {
      toast('❌ 翻译失败: '+e.message);
    } finally {
      if (progressEl) progressEl.classList.remove('active');
    }
  } else {
    // No translation — try global lyrics cache first, fall back to direct download
    var cacheKey = getLyricsCacheKey(songId, platform);
    var rawCache = lyricsDataCache[cacheKey];
    if (rawCache && rawCache.loaded && rawCache.raw) {
      // Use cached raw lyrics — skip server round-trip
      var rBlob = new Blob([rawCache.raw], { type: 'text/plain;charset=utf-8' });
      var ra = document.createElement('a');
      ra.href = URL.createObjectURL(rBlob);
      ra.download = (title && title !== 'Unknown' ? title : 'song') + '.lrc';
      document.body.appendChild(ra); ra.click(); document.body.removeChild(ra);
      URL.revokeObjectURL(ra.href);
      toast('📝 LRC 歌词已下载（使用缓存）');
    } else {
      // Fall back to direct server download
      var a = document.createElement('a');
      a.href = url;
      a.download = '';
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      toast('📝 LRC 歌词下载已开始');
    }
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

async function playCurrentSong(silent) {
  if (!currentSong) return;
  var cs = currentSong;
  if (!silent) toast('正在获取音源...');
  // Clear old lyrics display & preload lyrics for this song
  document.getElementById('playerLyricsCurrent').textContent = '';
  document.getElementById('playerLyricsNext').textContent = '';
  ensureLyricsLoaded(cs.songId, cs.platform);
  document.getElementById('playerBar').classList.add('loading');
  try {
    var resp = await fetch('/api/p/'+cs.songId+'?platform='+cs.platform+'&id='+cs.songId);
    var data = await resp.json();
    if (data.success && data.url) {
      audio.src = data.url;
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
      audio.play().catch(function(e){ if (!silent) toast('播放失败: '+e.message); });
      document.getElementById('playerTitle').textContent = cs.title;
      document.getElementById('playerArtist').textContent = cs.artist;
      if (!silent) toast('▶ 正在播放: ' + cs.title);
      updatePlayingIndicator(cs.songId);
    } else {
      if (!silent) toast('⚠ 暂无可用音源，请再次点击播放按钮重试');
    }
  } catch(e) {
    if (!silent) toast('❌ 获取失败，请再次点击播放按钮重试');
  } finally {
    document.getElementById('playerBar').classList.remove('loading');
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

function updatePlayingIndicator(songId) {
  document.querySelectorAll('.song-item.playing').forEach(function(el) {
    el.classList.remove('playing');
  });
  if (songId) {
    var el = document.getElementById('song-'+songId);
    if (el) el.classList.add('playing');
  }
}

var scrollTicking = false;
window.addEventListener('scroll', function() {
  if (!scrollTicking) {
    window.requestAnimationFrame(function() {
      var btn = document.getElementById('backToTop');
      if (btn) {
        if (window.scrollY > 400) {
          btn.classList.add('visible');
        } else {
          btn.classList.remove('visible');
        }
      }
      scrollTicking = false;
    });
    scrollTicking = true;
  }
}, { passive: true });

function scrollToTop() {
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

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
  await toggleSong(prev.id, prev.platform, idx - 1, false);
  var el = document.getElementById('song-'+prev.id);
  if (el) el.scrollIntoView({behavior:'smooth',block:'center'});
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
  await toggleSong(next.id, next.platform, idx + 1, false);
  var el = document.getElementById('song-'+next.id);
  if (el) el.scrollIntoView({behavior:'smooth',block:'center'});
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
