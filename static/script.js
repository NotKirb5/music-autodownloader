document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("download-form");
  const urlInput = document.getElementById("url");
  const playlistInput = document.getElementById("playlist");
  const submitBtn = document.getElementById("submit-btn");
  const progressCard = document.getElementById("progress-card");
  const progressBar = document.getElementById("progress-bar");
  const progressText = document.getElementById("progress-text");
  const trackList = document.getElementById("track-list");
  const libraryList = document.getElementById("library-list");
  const refreshLibBtn = document.getElementById("refresh-library");
  const cookiesBrowser = document.getElementById("cookies-browser");
  const cookiesFile = document.getElementById("cookies-file");
  const saveCookiesBtn = document.getElementById("save-cookies-btn");
  const cookiesStatus = document.getElementById("cookies-status");

  let currentSource = null;

  function closeStream() {
    if (currentSource) {
      currentSource.close();
      currentSource = null;
    }
  }

  function setFormDisabled(disabled) {
    urlInput.disabled = disabled;
    playlistInput.disabled = disabled;
    cookiesBrowser.disabled = disabled;
    cookiesFile.disabled = disabled;
    submitBtn.disabled = disabled;
    submitBtn.textContent = disabled ? "Downloading..." : "Download";
  }

  function trackIcon(cls, icon) {
    return `<span class="track-icon ${cls}">${icon}</span>`;
  }

  function addTrackItem(artist, title, icon, cls, detail) {
    const li = document.createElement("li");
    li.id = `track-${sanitizeId(artist, title)}`;
    li.innerHTML = `
      ${trackIcon(cls, icon)}
      <span class="track-info">
        <span class="track-name">${esc(artist)} &mdash; ${esc(title)}</span>
        ${detail ? `<span class="track-detail">${esc(detail)}</span>` : ""}
      </span>
    `;
    trackList.appendChild(li);
    li.scrollIntoView({ behavior: "smooth", block: "end" });
    return li;
  }

  function updateTrackItem(artist, title, icon, cls, detail) {
    const id = `track-${sanitizeId(artist, title)}`;
    let li = document.getElementById(id);
    if (!li) {
      li = addTrackItem(artist, title, icon, cls, detail);
    } else {
      const iconEl = li.querySelector(".track-icon");
      if (iconEl) {
        iconEl.className = `track-icon ${cls}`;
        iconEl.textContent = icon;
      }
      const detailEl = li.querySelector(".track-detail");
      if (detailEl && detail) detailEl.textContent = detail;
    }
    return li;
  }

  function esc(str) {
    const el = document.createElement("span");
    el.textContent = str;
    return el.innerHTML;
  }

  function sanitizeId(artist, title) {
    return `${artist}-${title}`.toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 60);
  }

  function handleSSEEvent(event) {
    if (!event.data) return;
    const data = JSON.parse(event.data);

    switch (event.type) {
      case "status":
        if (data.total) {
          const pct = data.current ? Math.round((data.current / data.total) * 100) : 0;
          progressBar.style.width = pct + "%";
        }
        progressText.textContent = data.message || "";
        break;

      case "track_progress":
        updateTrackItem(data.artist, data.title, "\u21bb", "spin", data.stage);
        break;

      case "track_skip":
        updateTrackItem(data.artist, data.title, "\u2714", "skip", "already in library");
        break;

      case "track_done":
        updateTrackItem(data.artist, data.title, "\u2714", "ok", data.playlist);
        break;

      case "track_error":
        updateTrackItem(data.artist, data.title, "\u2718", "err", data.error);
        break;

      case "done":
        progressBar.style.width = "100%";
        progressText.textContent = data.message || "Done!";
        setFormDisabled(false);
        closeStream();
        loadLibrary();
        break;

      case "error":
        progressText.textContent = "Error: " + (data.message || "Unknown error");
        setFormDisabled(false);
        closeStream();
        break;
    }
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = urlInput.value.trim();
    if (!url) return;

    closeStream();
    setFormDisabled(true);
    progressCard.hidden = false;
    trackList.innerHTML = "";
    progressBar.style.width = "0%";
    progressText.textContent = "Starting...";

    try {
      const res = await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url,
          playlist: playlistInput.value.trim(),
          cookies_from_browser: cookiesBrowser.value,
          cookies_file: cookiesFile.value.trim(),
        }),
      });
      const body = await res.json();
      if (!res.ok || body.error) {
        progressText.textContent = "Error: " + (body.error || "Request failed");
        setFormDisabled(false);
        return;
      }

      const src = new EventSource("/api/stream/" + body.task_id);
      currentSource = src;

      src.addEventListener("status", handleSSEEvent);
      src.addEventListener("track_progress", handleSSEEvent);
      src.addEventListener("track_skip", handleSSEEvent);
      src.addEventListener("track_done", handleSSEEvent);
      src.addEventListener("track_error", handleSSEEvent);
      src.addEventListener("done", handleSSEEvent);
      src.addEventListener("error", handleSSEEvent);
      src.addEventListener("heartbeat", () => {});

      src.onerror = () => {
        if (src.readyState === EventSource.CLOSED) {
          setFormDisabled(false);
          closeStream();
        }
      };
    } catch (err) {
      progressText.textContent = "Connection error: " + err.message;
      setFormDisabled(false);
    }
  });

  async function loadLibrary() {
    try {
      const res = await fetch("/api/library");
      const data = await res.json();
      if (!data.albums || data.albums.length === 0) {
        libraryList.innerHTML = '<p class="empty-state">No music in library yet.</p>';
        return;
      }
      libraryList.innerHTML = data.albums.map(album => `
        <div class="album-group">
          <div class="album-header">
            <span class="album-arrow">\u25B6</span>
            <span class="album-name">${esc(album.name)}</span>
            <span class="album-count">${album.songs.length} track${album.songs.length !== 1 ? "s" : ""}</span>
          </div>
          <div class="album-songs" style="display:none">
            ${album.songs.map(s => `
              <div class="library-item">
                <span class="library-name">${esc(s.name)}</span>
              </div>
            `).join("")}
          </div>
        </div>
      `).join("");
      libraryList.querySelectorAll(".album-header").forEach(header => {
        header.addEventListener("click", () => {
          const songs = header.nextElementSibling;
          const arrow = header.querySelector(".album-arrow");
          if (songs.style.display === "none") {
            songs.style.display = "block";
            arrow.textContent = "\u25BC";
          } else {
            songs.style.display = "none";
            arrow.textContent = "\u25B6";
          }
        });
      });
    } catch {
      libraryList.innerHTML = '<p class="empty-state">Failed to load library.</p>';
    }
  }

  async function loadConfig() {
    try {
      const res = await fetch("/api/config");
      const cfg = await res.json();
      if (cfg.cookies_from_browser) cookiesBrowser.value = cfg.cookies_from_browser;
      if (cfg.cookies_file) cookiesFile.value = cfg.cookies_file;
    } catch {}
  }

  async function saveConfig() {
    try {
      const res = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cookies_from_browser: cookiesBrowser.value,
          cookies_file: cookiesFile.value.trim(),
        }),
      });
      if (res.ok) {
        cookiesStatus.textContent = "Saved.";
        setTimeout(() => { cookiesStatus.textContent = ""; }, 2000);
      }
    } catch {
      cookiesStatus.textContent = "Save failed.";
    }
  }

  saveCookiesBtn.addEventListener("click", saveConfig);
  refreshLibBtn.addEventListener("click", loadLibrary);
  loadConfig();
  loadLibrary();
});
