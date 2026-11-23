// ArtFrame Studio - browser-native ES module, no build step.
const state = { file: null, style: "comic", view: "frame", current: null };

const $ = (sel) => document.querySelector(sel);
const drop = $("#drop"), photoInput = $("#photo"), preview = $("#preview"), dropText = $("#dropText");
const goBtn = $("#go"), pushBtn = $("#push"), statusEl = $("#status"), metaEl = $("#meta");
const viewEl = $("#view"), historyEl = $("#history"), pushTargetEl = $("#pushTarget");
const orientationEl = $("#orientation"), deleteBtn = $("#delete"), pushStatusEl = $("#pushStatus");

// Two independent status lines: #status belongs to the Generate panel (upload/remix),
// #pushStatus belongs to the Result panel (push progress, push result, deletion).
function setStatus(text, kind = "") {
  statusEl.textContent = text;
  statusEl.className = `status ${kind}`;
}

function setPushStatus(text, kind = "") {
  pushStatusEl.textContent = text;
  pushStatusEl.className = `status ${kind}`;
}

function selectStyle(key) {
  state.style = key;
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.classList.toggle("active", chip.dataset.style === key);
  });
}

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => selectStyle(chip.dataset.style));
});

function acceptFile(file) {
  if (!file || !file.type.startsWith("image/")) return setStatus("Please pick an image file.", "bad");
  state.file = file;
  preview.src = URL.createObjectURL(file);
  preview.hidden = false;
  dropText.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB`;
  goBtn.disabled = false;
  setStatus("");
}

photoInput.addEventListener("change", () => acceptFile(photoInput.files[0]));
["dragenter", "dragover"].forEach((evt) => drop.addEventListener(evt, (e) => {
  e.preventDefault(); drop.classList.add("over");
}));
["dragleave", "drop"].forEach((evt) => drop.addEventListener(evt, (e) => {
  e.preventDefault(); drop.classList.remove("over");
}));
drop.addEventListener("drop", (e) => acceptFile(e.dataTransfer.files[0]));

function showView() {
  if (!state.current) return;
  const url = state.current[`${state.view}_url`];
  viewEl.src = url || "";
  viewEl.hidden = !url;
  document.querySelectorAll(".tab[data-view]").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === state.view);
  });
}

document.querySelectorAll(".tab[data-view]").forEach((tab) => {
  tab.addEventListener("click", () => { state.view = tab.dataset.view; showView(); });
});

function describe(item) {
  const when = new Date(item.created * 1000).toLocaleString();
  return `${item.style_label} · ${when} · ${item.model} · $${item.cost_usd} · ${item.seconds}s`;
}

function setCurrent(payload) {
  state.current = payload;
  state.view = "frame";
  showView();
  const dims = payload && payload.width ? ` · ${payload.width}×${payload.height}` : "";
  metaEl.textContent = payload ? `${describe(payload)}${dims}` : "";
  // Push is attached to *this* result panel and always uploads what is displayed above.
  pushBtn.disabled = !(payload && payload.frame_url);
  deleteBtn.disabled = !payload;
  pushTargetEl.textContent = payload
    ? (payload.pushed_at
        ? `On the frame since ${new Date(payload.pushed_at * 1000).toLocaleTimeString()} — push again to replace it.`
        : `Will push: ${payload.style_label} (${payload.id}) — exactly what the preview shows.`)
    : "Pick a result to push.";
  if (!payload) setPushStatus("");
  if (payload) loadHistory();
}

goBtn.addEventListener("click", async () => {
  if (!state.file) return;
  const form = new FormData();
  form.append("photo", state.file);
  form.append("style", state.style);
  form.append("extra", $("#extra").value);
  form.append("model", $("#model").value);
  form.append("dither", $("#dither").value);
  form.append("quantize", $("#quantize").checked ? "1" : "0");
  goBtn.disabled = true;
  setStatus("🎨 Remixing… usually 5–15 seconds.", "busy");
  try {
    const resp = await fetch("/api/generate", { method: "POST", body: form });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    setCurrent(data);
    setStatus("Remix ready. Check the E Ink simulation, then push it from the Result panel.", "ok");
  } catch (err) {
    setStatus(`⚠️ ${err.message}`, "bad");
  } finally {
    goBtn.disabled = false;
  }
});

pushBtn.addEventListener("click", async () => {
  if (!state.current) return;
  const orientation = orientationEl.value;
  pushBtn.disabled = true;
  setPushStatus(`📤 Uploading ${state.current.id} to the frame…`, "busy");
  try {
    const resp = await fetch(`/api/push/${state.current.id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ orientation }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    const size = data.result?.uploaded_size;
    const dims = size ? ` (${size.width}×${size.height} uploaded)` : "";
    const how = data.result?.upload_rotate && data.result.upload_rotate !== "none"
      ? `, rotated ${data.result.upload_rotate.toUpperCase()}` : ", un-rotated";
    const label = `${state.current.style_label} (${state.current.id})`;
    state.current.pushed_at = data.pushed_at;
    setCurrent(state.current);  // refreshes the "on the frame since" line + gallery badges
    setPushStatus(`✅ ${label} is on ${data.device_id}${dims}${how} — the panel refreshes in ~30 s.`, "ok");
  } catch (err) {
    setPushStatus(`⚠️ ${err.message}`, "bad");
  } finally {
    pushBtn.disabled = false;
  }
});

deleteBtn.addEventListener("click", async () => {
  const item = state.current;
  if (!item) return;
  if (!confirm(`Delete ${item.style_label} (${item.id}) from the local gallery?\n\nThis removes the source, remix and E Ink files on the server. The frame keeps showing it.`)) {
    return;
  }
  try {
    const resp = await fetch(`/api/gallery/${item.id}`, { method: "DELETE" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    setPushStatus(`🗑️ Deleted ${item.id} (${data.removed_files.length} files, ${data.remaining} left).`, "ok");
    state.current = null;
    viewEl.hidden = true;
    viewEl.removeAttribute("src");
    metaEl.textContent = "";
    pushBtn.disabled = true;
    deleteBtn.disabled = true;
    pushTargetEl.textContent = "Pick a result to push.";
    loadHistory();
  } catch (err) {
    setPushStatus(`⚠️ ${err.message}`, "bad");
  }
});

async function loadHistory() {
  try {
    const resp = await fetch("/api/history?limit=24");
    const items = await resp.json();
    historyEl.replaceChildren(...items.map((item) => {
      const wrap = document.createElement("div");
      wrap.className = "thumb";
      wrap.title = describe(item);

      const img = document.createElement("img");
      img.src = item.eink_url || item.frame_url || item.result_url || "";
      img.alt = item.style_label;
      img.loading = "lazy";
      img.addEventListener("click", () => setCurrent(item));

      const badge = document.createElement("span");
      badge.textContent = item.pushed_at ? "✅" : "";

      const del = document.createElement("button");
      del.className = "thumb-delete";
      del.type = "button";
      del.textContent = "🗑";
      del.title = "Delete from gallery";
      del.addEventListener("click", async (evt) => {
        evt.stopPropagation();
        if (!confirm(`Delete ${item.style_label} (${item.id}) from the gallery?`)) return;
        const r = await fetch(`/api/gallery/${item.id}`, { method: "DELETE" });
        if (r.ok) {
          if (state.current && state.current.id === item.id) {
            state.current = null;
            viewEl.hidden = true;
            pushBtn.disabled = true;
            deleteBtn.disabled = true;
          }
          loadHistory();
        }
      });

      wrap.append(img, badge, del);
      return wrap;
    }));
  } catch { /* gallery is best-effort */ }
}

loadHistory();
