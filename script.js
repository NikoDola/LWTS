// Synced Lyrics Player frontend.
// Submits a YouTube URL to the backend, then plays the audio while
// highlighting the current lyric line in time with playback.

const form = document.getElementById("form");
const urlInput = document.getElementById("url");
const goBtn = document.getElementById("go");
const statusEl = document.getElementById("status");
const procBar = document.getElementById("procBar");
const procFill = document.getElementById("procFill");
const playerEl = document.getElementById("player");
const metaEl = document.getElementById("meta");
const audio = document.getElementById("audio");
const lyricsEl = document.getElementById("lyrics");
const libraryWrap = document.getElementById("libraryWrap");
const libraryEl = document.getElementById("library");
const sharpenBtn = document.getElementById("sharpen");
const translateBtn = document.getElementById("translateBtn");
const editBtn = document.getElementById("editBtn");
const sharpenBar = document.getElementById("sharpenBar");
const sharpenFill = document.getElementById("sharpenFill");
const offsetVal = document.getElementById("offsetVal");
const setStartBtn = document.getElementById("setStart");
const wordModal = document.getElementById("wordModal");
const wordClose = document.getElementById("wordClose");
const wordTitle = document.getElementById("wordTitle");
const wordBody = document.getElementById("wordBody");
const wordReplay = document.getElementById("wordReplay");

let replayTime = 0;   // playback time to jump to when "Replay this word" is clicked

let currentSongId = null;   // audioId of the song currently loaded
let currentData = null;     // full payload of the loaded song
let currentOffset = 0;      // manual lyric-timing offset (seconds)
let editMode = false;       // when on, lyric lines become editable (fix mishearings)

let lines = [];          // [{ time, text }]
let lineNodes = [];      // matching line DOM nodes
// Flat, time-sorted list of every word across all lines:
//   { span, time, lineIndex }
// The highlighter walks this so it can advance one word at a time (and force
// short words to flash) across line boundaries.
let allWords = [];
let activeIndex = -1;     // currently active line
let displayedWord = -1;   // index into allWords currently shown red
let lastAdvanceAt = 0;    // performance.now() of the last word advance
let rafId = null;         // requestAnimationFrame handle

const LAST_LINE_SECONDS = 4;  // assumed length of the final line (no next line)
const MIN_FLASH_MS = 110;     // each forced/skipped word stays red at least this long
const SEEK_GAP_WORDS = 6;     // jump (no sweep) when more than this many words behind

const SOURCE_LABEL = {
  lrclib: "synced lyrics",
  whisper: "AI transcription",
  none: "no lyrics found",
};

function setStatus(msg, isError = false) {
  if (!msg) {
    statusEl.hidden = true;
    return;
  }
  statusEl.hidden = false;
  statusEl.textContent = msg;
  statusEl.classList.toggle("error", isError);
}

// Read a Server-Sent-Events response, calling onEvent(obj) per `data:` frame.
async function readSSE(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, sep).trim();
      buf = buf.slice(sep + 2);
      if (frame.startsWith("data:")) onEvent(JSON.parse(frame.slice(5).trim()));
    }
  }
}

// Top-of-page progress bar for the whole download→lyrics→sharpen pipeline.
// It eases past the last reported percent between real updates, so the bar
// keeps visibly moving even during steps the backend can't measure precisely.
const proc = {
  shown: 0, target: 0, timer: null,
  start() {
    if (!procBar || !procFill) return;   // tolerate a stale/partial page
    this.shown = 0; this.target = 0;
    procFill.style.width = "0%";
    procBar.hidden = false;
    clearInterval(this.timer);
    this.timer = setInterval(() => this._tick(), 350);
  },
  update(percent, label) {
    if (typeof percent === "number") this.target = Math.max(this.target, percent);
    if (label) setStatus(label);
  },
  _tick() {
    if (!procFill) return;
    const cap = Math.min(99, this.target + 8);   // creep a little past the last real %
    if (this.shown < cap) {
      this.shown += Math.max(0.3, (cap - this.shown) * 0.1);
      procFill.style.width = `${Math.min(this.shown, 99).toFixed(1)}%`;
    }
  },
  finish() {
    clearInterval(this.timer); this.timer = null;
    if (!procBar || !procFill) return;
    procFill.style.width = "100%";
    setTimeout(() => { procBar.hidden = true; procFill.style.width = "0%"; }, 500);
  },
  fail() {
    clearInterval(this.timer); this.timer = null;
    if (!procBar || !procFill) return;
    procBar.hidden = true; procFill.style.width = "0%";
  },
};

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = urlInput.value.trim();
  if (!url) return;

  goBtn.disabled = true;
  playerEl.hidden = true;

  try {
    proc.start();
    proc.update(2, "Starting…");
    const res = await fetch("/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Request failed (${res.status})`);
    }

    let finalSong = null;
    await readSSE(res, (ev) => {
      if (ev.phase === "error") throw new Error(ev.message || "Processing failed.");
      if (ev.phase === "done") { finalSong = ev.song; return; }
      proc.update(ev.percent, ev.detail || "Working…");
    });
    if (!finalSong) throw new Error("Server finished without returning a song.");

    proc.update(100, "Done");
    proc.finish();
    loadSong(finalSong);
    setStatus(finalSong.warning || "");
    if (finalSong.warning) statusEl.classList.add("error");
    urlInput.value = "";
    loadLibrary();          // newly saved song now appears as a card
  } catch (err) {
    proc.fail();
    setStatus(err.message || "Something went wrong.", true);
  } finally {
    goBtn.disabled = false;
  }
});

// --- Saved-songs library -------------------------------------------------

async function loadLibrary() {
  try {
    const res = await fetch("/library");
    if (!res.ok) return;
    const songs = await res.json();
    renderLibrary(songs);
  } catch {
    /* ignore — library is non-critical */
  }
}

function renderLibrary(songs) {
  libraryEl.innerHTML = "";
  if (!songs.length) {
    libraryWrap.hidden = true;
    return;
  }
  libraryWrap.hidden = false;

  songs.forEach((s) => {
    const card = document.createElement("div");
    card.className = "card";
    card.title = "Play this song";

    const title = s.track || s.title || "Unknown";
    const artist = s.artist || "Unknown artist";

    // Show the real cover art when we have it; if the image fails to load,
    // fall back to the 🎵 note icon (older songs have no saved thumbnail).
    const art = s.thumbnail
      ? `<div class="art"><img src="${escapeHtml(s.thumbnail)}" alt="" loading="lazy" ` +
        `onerror="this.parentNode.classList.add('art-fallback');this.remove();"></div>`
      : `<div class="art art-fallback">🎵</div>`;

    card.innerHTML =
      `<button class="card-del" title="Delete this song" aria-label="Delete">✕</button>` +
      art +
      `<div class="card-title">${escapeHtml(title)}</div>` +
      `<div class="card-artist">${escapeHtml(artist)}</div>`;

    card.addEventListener("click", () => playFromLibrary(s.audioId));
    card.querySelector(".card-del").addEventListener("click", (e) => {
      e.stopPropagation();                // don't trigger "play" on the card
      deleteFromLibrary(s.audioId, title);
    });
    libraryEl.appendChild(card);
  });
}

async function deleteFromLibrary(audioId, title) {
  if (!confirm(`Delete "${title}" from your songs?`)) return;
  try {
    const res = await fetch(`/song/${encodeURIComponent(audioId)}`, { method: "DELETE" });
    if (!res.ok) throw new Error("Could not delete that song.");
    // If the deleted song is the one currently open, close the player.
    if (currentSongId === audioId) {
      audio.pause();
      audio.removeAttribute("src");
      playerEl.hidden = true;
      currentSongId = null;
    }
    loadLibrary();
  } catch (err) {
    setStatus(err.message || "Could not delete that song.", true);
  }
}

async function playFromLibrary(audioId) {
  setStatus("Loading saved song…");
  try {
    const res = await fetch(`/song/${encodeURIComponent(audioId)}`);
    if (!res.ok) throw new Error("Could not load that song.");
    const data = await res.json();
    loadSong(data);
    setStatus("");
  } catch (err) {
    setStatus(err.message || "Could not load that song.", true);
  }
}

function loadSong(data) {
  lines = Array.isArray(data.lyrics) ? data.lyrics : [];
  activeIndex = -1;
  displayedWord = -1;
  lastAdvanceAt = 0;
  currentData = data;
  currentOffset = Number(data.offset) || 0;
  updateOffsetLabel();
  lyricsEl.classList.remove("show-translation");   // start hidden each load

  const name = data.track || data.title || "Unknown";
  const artist = data.artist ? `${data.artist} — ` : "";
  const label = SOURCE_LABEL[data.source] || data.source;
  metaEl.innerHTML = `${escapeHtml(artist + name)}<span class="source">${escapeHtml(label)}</span>`;

  audio.src = `/audio/${encodeURIComponent(data.audioId)}`;
  renderLyrics(data);

  currentSongId = data.audioId;
  setSharpenState(data);
  setTranslateState(data);
  setEditState(data);

  playerEl.hidden = false;
  audio.play().catch(() => { /* autoplay may be blocked; user can press play */ });
}

// Configure the "Sharpen timing" button for the current song.
function setSharpenState(data) {
  const hasLyrics = Array.isArray(data.lyrics) && data.lyrics.length > 0;
  if (!hasLyrics) {
    sharpenBtn.hidden = true;
    return;
  }
  sharpenBtn.hidden = false;
  sharpenBtn.disabled = !!data.aligned;
  if (data.aligned) {
    sharpenBtn.textContent = "🎯 Timing sharpened";
    sharpenBtn.classList.add("aligned");
  } else {
    sharpenBtn.textContent = "🎯 Sharpen timing (AI, slow once)";
    sharpenBtn.classList.remove("aligned");
  }
}

function setProgress(percent, label) {
  sharpenBar.hidden = false;
  sharpenFill.style.width = `${percent}%`;
  sharpenBtn.textContent = `🎯 ${label} ${percent}%`;
}

function handleAlignEvent(ev) {
  if (ev.phase === "error") {
    throw new Error(ev.message || "Alignment failed.");
  }
  if (ev.phase === "done") {
    setProgress(100, "Done");
    const at = audio.currentTime;
    loadSong(ev.song);            // refreshes lyrics + button (now "sharpened")
    audio.currentTime = at;       // keep the listener's place
    sharpenBar.hidden = true;
    setStatus("");
    return;
  }
  // Every other phase (loading / syncing / separating / aligning) just shows
  // its own description from the backend, so nothing is ever silently ignored.
  setProgress(ev.percent, ev.detail || "Working…");
}

sharpenBtn.addEventListener("click", async () => {
  if (!currentSongId) return;
  sharpenBtn.disabled = true;
  setProgress(0, "Starting…");
  setStatus("Sharpening timing with AI — this runs once per song, then it's saved.");
  try {
    const res = await fetch(`/align/${encodeURIComponent(currentSongId)}`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Alignment failed (${res.status})`);
    }
    await readSSE(res, handleAlignEvent);
  } catch (err) {
    setStatus(err.message || "Alignment failed.", true);
    sharpenBar.hidden = true;
    sharpenBtn.disabled = false;
    sharpenBtn.textContent = "🎯 Sharpen timing (AI, slow once)";
  }
});

// --- whole-song translation ---------------------------------------------

function isTranslated(data) {
  return Array.isArray(data && data.translations) && data.translations.length > 0;
}

function setTranslateState(data) {
  const hasLyrics = Array.isArray(data.lyrics) && data.lyrics.length > 0;
  translateBtn.hidden = !hasLyrics;
  if (!hasLyrics) return;
  // Translation is Italian-only for now — make that clear for other languages.
  if (data.language && data.language !== "it") {
    translateBtn.disabled = true;
    translateBtn.textContent = `🌐 ${data.languageName || "This language"} not supported yet`;
    return;
  }
  translateBtn.disabled = false;
  if (isTranslated(data)) {
    const showing = lyricsEl.classList.contains("show-translation");
    translateBtn.textContent = showing ? "🌐 Hide translation" : "🌐 Show translation";
  } else {
    translateBtn.textContent = "🌐 Translate song";
  }
}

translateBtn.addEventListener("click", () => {
  // Already translated -> just toggle the inline translation on/off (no API).
  if (isTranslated(currentData)) {
    const showing = lyricsEl.classList.toggle("show-translation");
    translateBtn.textContent = showing ? "🌐 Hide translation" : "🌐 Show translation";
    return;
  }
  runTranslate();
});

async function runTranslate() {
  if (!currentSongId) return;
  translateBtn.disabled = true;
  sharpenBtn.disabled = true;
  setProgress(0, "Starting…");
  setStatus("Translating the whole song — one batched pass, saved after (slow on a local model).");
  try {
    const res = await fetch(`/translate/${encodeURIComponent(currentSongId)}`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Translation failed (${res.status})`);
    }
    await readSSE(res, handleTranslateEvent);
  } catch (err) {
    setStatus(err.message || "Translation failed.", true);
    sharpenBar.hidden = true;
    sharpenBtn.disabled = false;
    setTranslateState(currentData);
  }
}

function handleTranslateEvent(ev) {
  if (ev.phase === "error") throw new Error(ev.message || "Translation failed.");
  if (ev.phase === "done") {
    setProgress(100, "Done");
    const at = audio.currentTime;
    loadSong(ev.song);                  // re-renders lyrics with translations
    audio.currentTime = at;
    lyricsEl.classList.add("show-translation");  // reveal what we just made
    setTranslateState(currentData);
    sharpenBar.hidden = true;
    sharpenBtn.disabled = false;
    setStatus("");
    return;
  }
  setProgress(ev.percent, ev.detail || "Translating…");
}

// --- manual lyric editing (fix AI mishearings) --------------------------

function setEditState(data) {
  const hasLyrics = Array.isArray(data.lyrics) && data.lyrics.length > 0;
  editBtn.hidden = !hasLyrics;
  if (!hasLyrics && editMode) toggleEditMode(false);
}

function toggleEditMode(on) {
  editMode = on;
  editBtn.textContent = on ? "✓ Done editing" : "✏️ Edit lyrics";
  editBtn.classList.toggle("aligned", on);
  lyricsEl.classList.toggle("editing", on);
  if (on) lyricsEl.classList.remove("show-translation");  // less clutter while editing
  // Re-render so lines switch between word-spans and editable inputs.
  displayedWord = -1;
  activeIndex = -1;
  if (currentData) renderLyrics(currentData);
  setTranslateState(currentData);
}

editBtn.addEventListener("click", () => {
  if (!currentSongId) return;
  toggleEditMode(!editMode);
});

// Persist one corrected line, then refresh from the saved payload.
async function saveLineEdit(index, text) {
  if (!currentSongId) return;
  try {
    const res = await fetch(`/lyrics/${encodeURIComponent(currentSongId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index, text }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Save failed (${res.status})`);
    }
    const song = await res.json();
    const at = audio.currentTime;
    loadSong(song);            // re-renders with the corrected line
    audio.currentTime = at;    // keep the listener's place
    toggleEditMode(true);      // loadSong reset things; stay in edit mode
  } catch (err) {
    setStatus(err.message || "Could not save your edit.", true);
  }
}

// --- manual intro-sync offset -------------------------------------------

function updateOffsetLabel() {
  offsetVal.textContent = `${currentOffset >= 0 ? "+" : ""}${currentOffset.toFixed(1)}s`;
}

async function applyOffset(newOffset) {
  if (!currentData) return;
  currentOffset = Math.round(newOffset * 10) / 10;   // 0.1s steps
  updateOffsetLabel();
  // Re-render lyrics with the new offset, keeping playback position.
  displayedWord = -1;
  activeIndex = -1;
  renderLyrics(currentData);
  // Persist for next time.
  try {
    await fetch(`/offset/${encodeURIComponent(currentSongId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ offset: currentOffset }),
    });
    if (currentData) currentData.offset = currentOffset;
  } catch { /* non-critical */ }
}

document.querySelectorAll(".sync-btn[data-nudge]").forEach((btn) => {
  btn.addEventListener("click", () => {
    applyOffset(currentOffset + parseFloat(btn.dataset.nudge));
  });
});

setStartBtn.addEventListener("click", () => {
  if (!currentData || !lines.length) return;
  // Shift so the first lyric line lands at the current playback position.
  applyOffset(audio.currentTime - lines[0].time);
});

function renderLyrics(data) {
  lyricsEl.innerHTML = "";
  lineNodes = [];
  allWords = [];

  if (!lines.length) {
    const msg = data.instrumental
      ? "🎹 Instrumental — no lyrics."
      : "No lyrics available for this track.";
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = msg;
    lyricsEl.appendChild(p);
    return;
  }

  lines.forEach((line, i) => {
    const div = document.createElement("div");
    div.className = "line";

    // Edit mode: render the line as a text field instead of clickable words.
    if (editMode) {
      const input = document.createElement("input");
      input.type = "text";
      input.className = "line-edit";
      input.value = line.text || "";
      const commit = () => {
        const next = input.value.trim();
        if (next && next !== (line.text || "").trim()) saveLineEdit(i, next);
      };
      input.addEventListener("blur", commit);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); }
        if (e.key === "Escape") { input.value = line.text || ""; input.blur(); }
      });
      div.appendChild(input);
      lyricsEl.appendChild(div);
      lineNodes.push(div);
      return;
    }

    div.addEventListener("click", () => {
      audio.currentTime = line.time + currentOffset;
      audio.play().catch(() => {});
    });

    // End of this line = start of the next line (or a default for the last one).
    const lineStart = line.time;
    const lineEnd = i + 1 < lines.length
      ? lines[i + 1].time
      : lineStart + LAST_LINE_SECONDS;
    const span = Math.max(0.001, lineEnd - lineStart);

    // Prefer real per-word times from forced alignment when available;
    // otherwise fall back to evenly distributing words across the line.
    const aligned = Array.isArray(line.words) && line.words.length > 0;
    const tokens = aligned
      ? line.words.map((w) => w.text)
      : (line.text || "").split(/\s+/).filter(Boolean);

    if (tokens.length === 0) {
      div.textContent = "♪";
    } else {
      tokens.forEach((tok, j) => {
        const w = document.createElement("span");
        w.className = "word";
        w.textContent = tok;
        div.appendChild(w);
        if (j < tokens.length - 1) div.appendChild(document.createTextNode(" "));
        const baseTime = aligned
          ? line.words[j].time
          : lineStart + (j / tokens.length) * span;
        const wordTime = baseTime + currentOffset;
        // Click a word -> pause and open the translation popup.
        const sentence = line.text || "";
        w.addEventListener("click", (e) => {
          e.stopPropagation();            // don't fall back to the line's seek
          openWordModal(tok, sentence, wordTime);
        });
        allWords.push({ span: w, time: wordTime, lineIndex: i });
      });
    }

    // Whole-song translation (shown under the line when toggled on).
    const trans = (data.translations || [])[i];
    if (trans) {
      const td = document.createElement("div");
      td.className = "line-trans";
      td.textContent = trans;
      div.appendChild(td);
    }

    lyricsEl.appendChild(div);
    lineNodes.push(div);
  });

  // Keep words globally sorted by time (lines are already time-ordered, but
  // aligned word times can occasionally cross a line boundary).
  allWords.sort((a, b) => a.time - b.time);
}

// Binary search for the last word whose time <= t.
function wordIndexForTime(t) {
  let lo = 0, hi = allWords.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (allWords[mid].time <= t) {
      ans = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return ans;
}

// Make word `idx` the (only) current red word, updating the active line +
// scroll when we cross into a new line. `idx === -1` clears the highlight.
function showWord(idx) {
  if (idx === displayedWord) return;
  if (allWords[displayedWord]) allWords[displayedWord].span.classList.remove("sung");
  displayedWord = idx;

  const word = allWords[displayedWord];
  if (!word) return;

  // Restart the pop animation even when re-highlighting in quick succession.
  word.span.classList.remove("sung");
  void word.span.offsetWidth;          // force reflow so the keyframes replay
  word.span.classList.add("sung");

  if (word.lineIndex !== activeIndex) {
    if (lineNodes[activeIndex]) lineNodes[activeIndex].classList.remove("active");
    activeIndex = word.lineIndex;
    const node = lineNodes[activeIndex];
    if (node) {
      node.classList.add("active");
      node.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
}

// Animation-frame loop (~60 Hz, vs ~4 Hz for `timeupdate`) so short words aren't
// missed. The minimum-dwell catch-up forces every skipped word to flash red for
// at least MIN_FLASH_MS, so fast connected singing never silently skips a word.
function tick() {
  if (allWords.length) {
    const target = wordIndexForTime(audio.currentTime);

    if (target < displayedWord) {
      showWord(target);                       // seeked backwards
      lastAdvanceAt = performance.now();
    } else if (target > displayedWord) {
      if (target - displayedWord > SEEK_GAP_WORDS) {
        showWord(target);                     // big jump (seek / line click)
        lastAdvanceAt = performance.now();
      } else {
        const now = performance.now();
        if (now - lastAdvanceAt >= MIN_FLASH_MS) {
          showWord(displayedWord + 1);        // step one word, guaranteeing a pop
          lastAdvanceAt = now;
        }
      }
    }
  }
  rafId = requestAnimationFrame(tick);
}

function startTicking() {
  if (rafId === null) {
    lastAdvanceAt = performance.now();
    rafId = requestAnimationFrame(tick);
  }
}

function stopTicking() {
  if (rafId !== null) {
    cancelAnimationFrame(rafId);
    rafId = null;
  }
}

audio.addEventListener("play", startTicking);
audio.addEventListener("playing", startTicking);
audio.addEventListener("pause", stopTicking);
audio.addEventListener("ended", stopTicking);
// On a manual seek, snap straight to the right word (no sweep).
audio.addEventListener("seeking", () => {
  showWord(wordIndexForTime(audio.currentTime));
  lastAdvanceAt = performance.now();
});

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// --- word translation popup (language learning) -------------------------

// Click a word -> pause the song and show its translation + grammar.
async function openWordModal(rawWord, sentence, wordTime) {
  // Strip surrounding punctuation but keep internal apostrophes/hyphens.
  const word = rawWord.replace(/^[^\p{L}]+|[^\p{L}]+$/gu, "") || rawWord;
  replayTime = wordTime;
  audio.pause();

  wordTitle.textContent = word;
  wordReplay.hidden = false;
  wordModal.hidden = false;

  // Word lookups are Italian-only. For other languages, say so kindly and stop.
  const lang = currentData && currentData.language;
  if (lang && lang !== "it") {
    const name = (currentData && currentData.languageName) || "this language";
    wordBody.innerHTML =
      `<p class="word-unsupported">🌍 Word translations are available for <b>Italian</b> only right now.<br><br>` +
      `This song looks like <b>${escapeHtml(name)}</b>, so I can't look up its words yet — ` +
      `but you can still play along and replay the line.</p>`;
    return;
  }

  wordBody.innerHTML = `<p class="word-loading">Looking up “${escapeHtml(word)}”…<br><span style="font-size:.8rem">(a new word can take a moment; it's instant next time)</span></p>`;

  try {
    const res = await fetch("/word", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ word, sentence }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Lookup failed.");
    renderWord(data);
  } catch (err) {
    wordBody.innerHTML = `<p class="word-error">${escapeHtml(err.message || "Lookup failed.")}</p>`;
    wordReplay.hidden = true;
  }
}

function renderWord(d) {
  const meanings = Array.isArray(d.meanings) && d.meanings.length
    ? d.meanings
    : (d.translation ? [d.translation] : []);
  // Dictionary results carry a per-meaning examples[]; AI results carry one
  // combined example string — normalise both to the same shape.
  const examples = Array.isArray(d.examples) ? d.examples
    : (d.example ? [{ meaning: d.translation, text: d.example, en: "" }] : []);
  const exampleFor = (m) => examples.find((e) => e.meaning === m);

  const exHtml = (e) => e
    ? `<div class="sense-ex"><em>${escapeHtml(e.text)}</em>` +
      (e.en ? ` — ${escapeHtml(e.en)}` : "") + `</div>`
    : "";

  const rows = [
    ["Part of speech", d.part_of_speech],
    ["Gender", d.gender],
    ["Number", d.number],
    ["Base form", d.lemma],
    ["Tense", d.tense],
    ["Mood", d.mood],
    ["Person", d.person],
  ].filter(([, v]) => v && v !== "-" && v !== "—" && v.trim() !== "");
  const rowsHtml = rows
    .map(([k, v]) => `<div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(v)}</div>`)
    .join("");

  // Multiple meanings (come -> how / as / like): list every one with its example.
  let meaningsHtml = "";
  if (meanings.length > 1) {
    meaningsHtml =
      `<div class="word-meaning-label">All meanings</div>` +
      `<ol class="word-senses">` +
      meanings.map((m) =>
        `<li><span class="sense-g">${escapeHtml(m)}</span>${exHtml(exampleFor(m))}</li>`
      ).join("") +
      `</ol>`;
  }
  // Single-meaning words: just show the one example, if any.
  const singleEx = meanings.length <= 1 && examples[0]
    ? `<div class="word-extra"><span class="label">Example</span>${exHtml(examples[0])}</div>`
    : "";

  wordBody.innerHTML =
    (d.language ? `<div class="word-lang">${escapeHtml(d.language)}</div>` : "") +
    `<div class="word-meaning-label">English meaning</div>` +
    `<div class="word-translation">${escapeHtml(d.translation || meanings[0] || "")}</div>` +
    (d.detail ? `<div class="word-detail">${escapeHtml(d.detail)}</div>` : "") +
    meaningsHtml +
    singleEx +
    (rowsHtml ? `<div class="word-rows">${rowsHtml}</div>` : "") +
    (d.note ? `<div class="word-extra"><span class="label">Tip</span><br>${escapeHtml(d.note)}</div>` : "");
}

function closeWordModal() {
  wordModal.hidden = true;
}

wordClose.addEventListener("click", closeWordModal);
wordModal.addEventListener("click", (e) => {
  if (e.target === wordModal) closeWordModal();   // backdrop click
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !wordModal.hidden) closeWordModal();
});
wordReplay.addEventListener("click", () => {
  closeWordModal();
  audio.currentTime = replayTime;
  audio.play().catch(() => {});
});

// Show any previously saved songs as soon as the page loads.
loadLibrary();
