// iMovie Script Studio - 前端應用邏輯

let currentMediaItems = [];
let currentScriptData = null;

// DOM Elements - Source Tabs
const tabApplePhotos = document.getElementById('tab-apple-photos');
const tabLocalFolder = document.getElementById('tab-local-folder');
const tabUpload = document.getElementById('tab-upload');

const panelApplePhotos = document.getElementById('panel-apple-photos');
const panelLocalFolder = document.getElementById('panel-local-folder');
const panelUpload = document.getElementById('panel-upload');

const selectAppleAlbum = document.getElementById('select-apple-album');
const btnRefreshAlbums = document.getElementById('btn-refresh-albums');
const btnScanAppleAlbum = document.getElementById('btn-scan-apple-album');

const inputFolderPath = document.getElementById('input-folder-path');
const btnScanFolder = document.getElementById('btn-scan-folder');
const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
const inputUserPrompt = document.getElementById('input-user-prompt');
const selectStyle = document.getElementById('select-style');
const selectRatio = document.getElementById('select-ratio');
const inputDuration = document.getElementById('input-duration');
const btnGenerateAll = document.getElementById('btn-generate-all');
const btnDemoData = document.getElementById('btn-demo-data');

const sectionMediaTimeline = document.getElementById('section-media-timeline');
const mediaGridContainer = document.getElementById('media-grid-container');
const mediaCountBadge = document.getElementById('media-count');

const sectionScriptResult = document.getElementById('section-script-result');
const scriptSubtitle = document.getElementById('script-subtitle');
const scriptThemeText = document.getElementById('script-theme-text');
const scriptSoundtrackText = document.getElementById('script-soundtrack-text');
const storyboardListContainer = document.getElementById('storyboard-list-container');

const btnExportMd = document.getElementById('btn-export-md');
const btnExportJson = document.getElementById('btn-export-json');
const btnExportFcpxml = document.getElementById('btn-export-fcpxml');

const loadingOverlay = document.getElementById('loading-overlay');
const loadingTitle = document.getElementById('loading-title');
const loadingDesc = document.getElementById('loading-desc');

// 初始事件綁定
document.addEventListener('DOMContentLoaded', () => {
  // 來源切換 Tabs
  setupSourceTabs();

  // Apple Photos
  btnRefreshAlbums.addEventListener('click', loadAppleAlbums);
  btnScanAppleAlbum.addEventListener('click', handleScanAppleAlbum);
  loadAppleAlbums(); // 初始載入相簿

  btnScanFolder.addEventListener('click', handleScanFolder);
  btnGenerateAll.addEventListener('click', handleGenerateAll);
  btnDemoData.addEventListener('click', loadDemoData);

  // 拖曳上傳
  dropZone.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', handleFileSelect);

  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('dragover');
  });

  dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
  });

  dropZone.addEventListener('drop', handleFileDrop);

  // 匯出按鈕
  btnExportMd.addEventListener('click', () => exportScript('markdown', 'movie_script.md'));
  btnExportJson.addEventListener('click', () => exportScript('json', 'movie_script.json'));
  btnExportFcpxml.addEventListener('click', () => exportScript('fcpxml', 'movie_script.fcpxml'));
});

// 切換來源選項卡
function setupSourceTabs() {
  const tabs = [
    { btn: tabApplePhotos, panel: panelApplePhotos },
    { btn: tabLocalFolder, panel: panelLocalFolder },
    { btn: tabUpload, panel: panelUpload }
  ];

  tabs.forEach(tab => {
    tab.btn.addEventListener('click', () => {
      tabs.forEach(t => {
        t.btn.classList.remove('active');
        t.panel.style.display = 'none';
      });
      tab.btn.classList.add('active');
      tab.panel.style.display = 'flex';
    });
  });
}

// 載入 Apple Photos 相簿列表
async function loadAppleAlbums() {
  selectAppleAlbum.innerHTML = '<option value="">正在載入 Apple 相簿...</option>';
  try {
    const res = await fetch('/api/apple-photos/albums');
    const data = await res.json();
    if (!res.ok) {
      selectAppleAlbum.innerHTML = `<option value="">⚠️ ${data.detail || '未支援/無權限'}</option>`;
      return;
    }

    const albums = data.albums || [];
    if (albums.length === 0) {
      selectAppleAlbum.innerHTML = '<option value="">（未發現相簿）</option>';
      return;
    }

    selectAppleAlbum.innerHTML = albums.map(a => 
      `<option value="${a.title}">📸 ${a.title} (${a.count} 張照片)</option>`
    ).join('');
  } catch (e) {
    selectAppleAlbum.innerHTML = '<option value="">⚠️ 載入 Apple 相簿失敗 (請確認 macOS 權限)</option>';
  }
}

// 掃描所選 Apple Photos 相簿
async function handleScanAppleAlbum() {
  const albumName = selectAppleAlbum.value;
  if (!albumName) {
    alert('請先選擇一個 Apple 相簿！');
    return;
  }

  showLoading('正在讀取 Apple 相簿...', `正在從「${albumName}」無損提取 iOS 說明欄、GPS 地名與 Live Photo 動態短片`);
  try {
    const res = await fetch('/api/apple-photos/scan-album', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ album_name: albumName, max_photos: 150, resolve_location: true })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '相簿讀取失敗');

    currentMediaItems = data.items;
    if (currentMediaItems.length === 0) {
      alert(`相簿「${albumName}」中未找到相片！`);
      return;
    }
    renderMediaTimeline(currentMediaItems);
    btnGenerateAll.disabled = currentMediaItems.length === 0;
  } catch (err) {
    alert(`相簿讀取失敗: ${err.message}`);
  } finally {
    hideLoading();
  }
}

// 顯示/隱藏 Loading
function showLoading(title, desc) {
  loadingTitle.textContent = title;
  loadingDesc.textContent = desc;
  loadingOverlay.style.display = 'flex';
}

function hideLoading() {
  loadingOverlay.style.display = 'none';
}

// 掃描本機資料夾
async function handleScanFolder() {
  const folderPath = inputFolderPath.value.trim();
  if (!folderPath) {
    alert('請輸入本機照片/影片所在的資料夾路徑！');
    return;
  }

  showLoading('正在掃描資料夾...', '正在提取照片 EXIF、iOS 說明欄、GPS 地名並配對 Live Photo');
  try {
    const res = await fetch('/api/scan-folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder_path: folderPath, resolve_location: true })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '掃描失敗');

    currentMediaItems = data.items;
    renderMediaTimeline(currentMediaItems);
    btnGenerateAll.disabled = currentMediaItems.length === 0;
  } catch (err) {
    alert(`掃描失敗: ${err.message}`);
  } finally {
    hideLoading();
  }
}

// 檔案拖曳與選擇上傳
async function handleFileDrop(e) {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  const files = e.dataTransfer.files;
  if (files.length > 0) uploadFiles(files);
}

function handleFileSelect(e) {
  const files = e.target.files;
  if (files.length > 0) uploadFiles(files);
}

async function uploadFiles(files) {
  const formData = new FormData();
  for (let i = 0; i < files.length; i++) {
    formData.append('files', files[i]);
  }

  showLoading('正在上傳並解析檔案...', '正在讀取照片 metadata、iOS 說明欄與 Live Photo 短片');
  try {
    const res = await fetch('/api/upload', {
      method: 'POST',
      body: formData
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '上傳解析失敗');

    currentMediaItems = data.items;
    renderMediaTimeline(currentMediaItems);
    btnGenerateAll.disabled = currentMediaItems.length === 0;
  } catch (err) {
    alert(`上傳失敗: ${err.message}`);
  } finally {
    hideLoading();
  }
}

// 渲染素材清單
function renderMediaTimeline(items) {
  mediaGridContainer.innerHTML = '';
  mediaCountBadge.textContent = items.length;
  sectionMediaTimeline.style.display = 'block';

  items.forEach((item, index) => {
    const card = document.createElement('div');
    card.className = 'media-card';

    const isLive = item.is_live_photo;
    const hasCaption = Boolean(item.caption && item.caption.trim());
    const locName = item.location ? item.location.short_location : null;

    // 縮圖 URL
    const thumbSrc = `/api/thumbnail?path=${encodeURIComponent(item.file_path)}&size=400`;

    // 檢查是否有視覺分析裁切座標
    let cropOverlayHtml = '';
    if (item.analysis && item.analysis.crop_suggestion && item.analysis.crop_suggestion.crop_box_normalized) {
      const [ymin, xmin, ymax, xmax] = item.analysis.crop_suggestion.crop_box_normalized;
      const top = (ymin * 100).toFixed(1);
      const left = (xmin * 100).toFixed(1);
      const width = ((xmax - xmin) * 100).toFixed(1);
      const height = ((ymax - ymin) * 100).toFixed(1);
      cropOverlayHtml = `<div class="crop-overlay-box" style="top: ${top}%; left: ${left}%; width: ${width}%; height: ${height}%;"></div>`;
    }

    card.innerHTML = `
      <div class="media-thumb-box">
        <img src="${thumbSrc}" alt="${item.file_name}" onerror="this.src=''; this.parentElement.innerHTML='<div class=\\'media-thumb-placeholder\\'>📷</div>';">
        ${cropOverlayHtml}
        <div class="thumb-tag">
          ${isLive ? '<span class="badge badge-live">Live Photo</span>' : ''}
          ${item.media_type === 'video' ? '<span class="badge badge-live">影片</span>' : ''}
        </div>
        <div class="thumb-idx">#${index + 1}</div>
      </div>
      <div class="media-info-body">
        <div class="media-filename" title="${item.file_name}">${item.file_name}</div>
        
        <div class="media-caption-box">
          <span class="caption-label">📝 iOS 說明欄記憶</span>
          <textarea class="form-input" style="font-size: 0.85rem; padding: 4px 6px;" rows="2" data-item-idx="${index}" placeholder="在此補充或編輯此照片的想法...">${item.caption || ''}</textarea>
        </div>

        ${item.analysis ? `
        <div style="background: rgba(99, 102, 241, 0.1); border-left: 3px solid var(--primary); padding: 6px 8px; border-radius: 4px; font-size: 0.8rem; margin: 4px 0;">
          <div style="color: #a5b4fc; font-weight: 600;">📐 ${item.analysis.shot_type || '中景'} ｜ ${item.analysis.composition || '黃金構圖'}</div>
          ${item.analysis.camera_motion_suggestion ? `<div style="color: #cbd5e1; margin-top: 2px;">🎥 運鏡：${item.analysis.camera_motion_suggestion.motion_type} (${item.analysis.camera_motion_suggestion.motion_description})</div>` : ''}
        </div>
        ` : ''}

        <div class="media-meta-tags">
          ${locName ? `<span>📍 ${locName}</span>` : '<span>📍 未定位</span>'}
          <span>🕒 ${item.creation_date_formatted || '未知時間'}</span>
          <span>📐 ${item.width} x ${item.height} ${isLive ? `(${item.live_photo_video ? 'Live動態' : 'Live'})` : ''}</span>
        </div>
      </div>
    `;

    // 即時同步修改的 caption
    const textarea = card.querySelector('textarea');
    textarea.addEventListener('input', (e) => {
      currentMediaItems[index].caption = e.target.value;
    });

    mediaGridContainer.appendChild(card);
  });

  // 平滑滾動到步驟 2
  sectionMediaTimeline.scrollIntoView({ behavior: 'smooth' });
}

// 一鍵執行：視覺分析 + 智慧劇本編譯
async function handleGenerateAll() {
  if (currentMediaItems.length === 0) return;

  const userPrompt = inputUserPrompt.value.trim() || '請幫我編寫一段溫馨感人的旅行生活紀錄片腳本';
  const style = selectStyle.value;
  const ratio = selectRatio.value;
  const duration = inputDuration.value ? parseInt(inputDuration.value) : null;

  // 1. 執行視覺分析與裁切計算
  showLoading('步驟 1/2: 正在進行多模態視覺與構圖裁切分析...', `針對目標比例 ${ratio} 計算最佳取景焦點、黃金構圖與 Ken Burns 鏡頭動態`);
  
  try {
    const analyzeRes = await fetch('/api/analyze-items', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items: currentMediaItems,
        target_aspect_ratio: ratio
      })
    });
    const analyzeData = await analyzeRes.json();
    if (!analyzeRes.ok) throw new Error(analyzeData.detail || '視覺分析失敗');
    
    currentMediaItems = analyzeData.items;
    // 更新素材卡片的裁切框
    renderMediaTimeline(currentMediaItems);

    // 2. 執行劇本與口白編譯
    showLoading('步驟 2/2: 正在融合照片回憶，編譯分鏡劇本與口白...', '根據您的需求 Prompt 與說明欄記憶，創作電影級分鏡、旁白與聲音設計');

    const scriptRes = await fetch('/api/generate-script', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items_with_analysis: currentMediaItems,
        user_prompt: userPrompt,
        target_duration: duration,
        style: style,
        target_aspect_ratio: ratio
      })
    });
    const scriptData = await scriptRes.json();
    if (!scriptRes.ok) throw new Error(scriptData.detail || '劇本生成失敗');

    currentScriptData = scriptData.script;
    renderStoryboardScript(currentScriptData);

  } catch (err) {
    alert(`生成失敗: ${err.message}`);
  } finally {
    hideLoading();
  }
}

// 渲染產出的分鏡腳本
function renderStoryboardScript(script) {
  sectionScriptResult.style.display = 'block';
  scriptSubtitle.textContent = `🎬 ${script.project_title} ｜ ${script.subtitle || ''}`;
  
  const loglineElem = document.getElementById('script-logline-text');
  if (loglineElem) loglineElem.textContent = script.narrative_logline || script.subtitle || '無特別指定';

  scriptThemeText.textContent = script.theme_summary || '-';

  const notesElem = document.getElementById('script-notes-text');
  if (notesElem) notesElem.textContent = script.director_notes || '請把握節奏呼吸感，在 Live Photo 動態轉定格時讓旁白自然切入。';

  const st = script.soundtrack_design || {};
  scriptSoundtrackText.textContent = `【氛圍】：${st.overall_mood || '溫馨'} ｜ 【推薦風格】：${st.recommended_tracks || 'Acoustic / Piano'} ｜ 【起伏】：${st.audio_dynamics || '流暢起承轉合'}`;

  storyboardListContainer.innerHTML = '';

  const shots = script.storyboard || [];
  shots.forEach((shot, index) => {
    const card = document.createElement('div');
    card.className = 'shot-card';

    // 尋找對應素材的縮圖
    const matchedMedia = currentMediaItems.find(m => m.file_name === shot.media_file);
    const thumbUrl = matchedMedia ? `/api/thumbnail?path=${encodeURIComponent(matchedMedia.file_path)}&size=300` : '';

    const isLive = shot.is_live_photo;

    card.innerHTML = `
      <div class="shot-visual-col">
        ${thumbUrl ? `<img src="${thumbUrl}" class="shot-preview-img" alt="${shot.media_file}">` : '<div class="shot-preview-img media-thumb-placeholder">🎞️</div>'}
        <span style="font-size: 0.75rem; color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
          ${shot.media_file} ${isLive ? '💫 Live Photo' : ''}
        </span>
      </div>

      <div class="shot-main-col">
        <div class="shot-title-row">
          <h3>#${shot.shot_index.toString().padStart(2, '0')} ${shot.scene_title}</h3>
          <span class="shot-duration-badge">⏱️ ${shot.duration_seconds} 秒</span>
        </div>

        <div class="shot-vo-box">
          <span class="vo-label">🎙️ 配音旁白口白 (Voiceover)</span>
          <textarea class="form-input shot-vo-text" rows="2" data-shot-idx="${index}">${shot.voiceover || ''}</textarea>
        </div>

        <div class="shot-details-grid">
          <span><strong>景別：</strong>${shot.shot_type || '中景'}</span>
          <span><strong>轉場：</strong>${shot.transition || '交叉溶解'}</span>
          <span><strong>鏡頭動態：</strong>${shot.camera_motion || 'Slow Zoom-in'}</span>
          <span><strong>裁切取景：</strong>${shot.crop_focus || '黃金三分點'}</span>
          ${isLive && shot.live_photo_usage ? `<span style="grid-column: 1 / -1; color: #f472b6;"><strong>💫 Live Photo 運用：</strong>${shot.live_photo_usage}</span>` : ''}
          <span style="grid-column: 1 / -1;"><strong>🎼 配樂情緒：</strong>${shot.bgm_cue || '溫柔背景音樂'}</span>
          ${shot.sfx_cue ? `<span style="grid-column: 1 / -1; color: #60a5fa;"><strong>🔊 音效 Cue 點：</strong>${shot.sfx_cue}</span>` : ''}
        </div>
      </div>
    `;

    // 即時修改口白
    const voTextarea = card.querySelector('.shot-vo-text');
    voTextarea.addEventListener('input', (e) => {
      currentScriptData.storyboard[index].voiceover = e.target.value;
    });

    storyboardListContainer.appendChild(card);
  });

  // 平滑滾動至腳本區
  sectionScriptResult.scrollIntoView({ behavior: 'smooth' });
}

// 匯出腳本檔案
async function exportScript(format, filename) {
  if (!currentScriptData) {
    alert('尚未生成腳本！');
    return;
  }

  try {
    const res = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        script_data: currentScriptData,
        format: format
      })
    });
    
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  } catch (err) {
    alert(`匯出失敗: ${err.message}`);
  }
}

// 載入 Demo 範例資料 (示範京都 5 天旅行回憶)
function loadDemoData() {
  inputUserPrompt.value = "這是我和另一半在京都的賞楓紀念，想剪一部約 1 分鐘的溫暖旅行紀錄片，口白要真誠細膩，融入每張照片寫下的回憶與當時的心情。";
  selectStyle.value = "自然感人旅行Vlog";
  selectRatio.value = "16:9";
  inputDuration.value = "75";

  currentMediaItems = [
    {
      "file_name": "IMG_4821.HEIC",
      "file_path": "/demo/IMG_4821.HEIC",
      "is_image": true,
      "is_video": false,
      "media_type": "live_photo",
      "is_live_photo": true,
      "live_video": { "file_name": "IMG_4821.MOV", "duration": 2.1, "width": 1920, "height": 1080 },
      "caption": "清晨剛抵達京都車站，微冷的海風吹過，兩個人拉著行李相視而笑，旅程正式開始！",
      "creation_date_formatted": "2024-11-12 08:30:15",
      "width": 4032,
      "height": 3024,
      "location": { "short_location": "日本 京都府 京都市 京都車站" }
    },
    {
      "file_name": "IMG_4855.HEIC",
      "file_path": "/demo/IMG_4855.HEIC",
      "is_image": true,
      "is_video": false,
      "media_type": "image",
      "is_live_photo": false,
      "caption": "二年坂的小巷子，兩旁古色古香的木造町家，午後陽光灑在石板路上好安靜。",
      "creation_date_formatted": "2024-11-12 13:45:00",
      "width": 4032,
      "height": 3024,
      "location": { "short_location": "日本 京都府 東山區 二年坂" }
    },
    {
      "file_name": "IMG_4890.HEIC",
      "file_path": "/demo/IMG_4890.HEIC",
      "is_image": true,
      "is_video": false,
      "media_type": "live_photo",
      "is_live_photo": true,
      "live_video": { "file_name": "IMG_4890.MOV", "duration": 1.8, "width": 1920, "height": 1080 },
      "caption": "站在清水舞台望出去，滿山火紅的楓葉隨風搖曳，你轉過頭遞給我熱熱的抹茶糰子。",
      "creation_date_formatted": "2024-11-12 16:15:20",
      "width": 4032,
      "height": 3024,
      "location": { "short_location": "日本 京都府 清水寺 音羽山" }
    },
    {
      "file_name": "IMG_4930.HEIC",
      "file_path": "/demo/IMG_4930.HEIC",
      "is_image": true,
      "is_video": false,
      "media_type": "image",
      "is_live_photo": false,
      "caption": "鴨川畔的晚霞，天空被染成粉紫色的漸層，河邊有人在彈吉他。",
      "creation_date_formatted": "2024-11-12 17:40:10",
      "width": 4032,
      "height": 3024,
      "location": { "short_location": "日本 京都府 鴨川 四條大橋" }
    },
    {
      "file_name": "IMG_4978.HEIC",
      "file_path": "/demo/IMG_4978.HEIC",
      "is_image": true,
      "is_video": false,
      "media_type": "live_photo",
      "is_live_photo": true,
      "live_video": { "file_name": "IMG_4978.MOV", "duration": 2.0, "width": 1920, "height": 1080 },
      "caption": "居酒屋門口暖黃的燈籠下，兩個人碰杯說：謝謝這趟完美的旅行。",
      "creation_date_formatted": "2024-11-12 20:30:45",
      "width": 4032,
      "height": 3024,
      "location": { "short_location": "日本 京都府 先斗町" }
    }
  ];

  renderMediaTimeline(currentMediaItems);
  btnGenerateAll.disabled = false;
}
