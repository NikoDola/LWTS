// Synced Lyrics Player frontend.
// Submits a YouTube URL to the backend, then plays the audio while
// highlighting the current lyric line in time with playback.

const form = document.getElementById("form");
const urlInput = document.getElementById("url");
const goBtn = document.getElementById("go");
const statusEl = document.getElementById("status");
const playerEl = document.getElementById("player");
const metaEl = document.getElementById("meta");
const audio = document.getElementById("audio");
const lyricsEl = document.getElementById("lyrics");
const libraryWrap = document.getElementById("libraryWrap");
const libraryEl = document.getElementById("library");
const sharpenBtn = document.getElementById("sharpen");
const sharpenBar = document.getElementById("sharpenBar");
const sharpenFill = document.getElementById("sharpenFill");
const offsetVal = document.getElementById("offsetVal");
const setStartBtn = document.getElementById("setStart");

let currentSongId = null;   // audioId of the song currently loaded
let currentData = null;     // full payload of the loaded song
let currentOffset = 0;      // manual lyric-timing offset (seconds)

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

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = urlInput.value.trim();
  if (!url) return;

  goBtn.disabled = true;
  playerEl.hidden = true;
  setStatus("Downloading audio and finding lyrics… (this can take a bit, and longer if AI transcription is needed)");

  try {
    const res = await fetch("/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Request failed (${res.status})`);
    }

    const data = await res.json();
    loadSong(data);
    setStatus(data.warning || "");
    if (data.warning) statusEl.classList.add("error");
    urlInput.value = "";
    loadLibrary();          // newly saved song now appears as a card
  } catch (err) {
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

    card.innerHTML =
      `<button class="card-del" title="Delete this song" aria-label="Delete">✕</button>` +
      `<div class="art">🎵</div>` +
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

  const name = data.track || data.title || "Unknown";
  const artist = data.artist ? `${data.artist} — ` : "";
  const label = SOURCE_LABEL[data.source] || data.source;
  metaEl.innerHTML = `${escapeHtml(artist + name)}<span class="source">${escapeHtml(label)}</span>`;

  audio.src = `/audio/${encodeURIComponent(data.audioId)}`;
  renderLyrics(data);

  currentSongId = data.audioId;
  setSharpenState(data);

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
    // Read the Server-Sent-Events stream of progress updates.
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
        if (frame.startsWith("data:")) {
          handleAlignEvent(JSON.parse(frame.slice(5).trim()));
        }
      }
    }
  } catch (err) {
    setStatus(err.message || "Alignment failed.", true);
    sharpenBar.hidden = true;
    sharpenBtn.disabled = false;
    sharpenBtn.textContent = "🎯 Sharpen timing (AI, slow once)";
  }
});

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
        // Click a word -> jump the track to exactly that word.
        w.addEventListener("click", (e) => {
          e.stopPropagation();            // don't fall back to the line's seek
          audio.currentTime = wordTime;
          audio.play().catch(() => {});
        });
        allWords.push({ span: w, time: wordTime, lineIndex: i });
      });
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

// Show any previously saved songs as soon as the page loads.
loadLibrary();
