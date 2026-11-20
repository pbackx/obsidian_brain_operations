// ArtFrame Studio - browser-native ES module, no build step.
const state = { file: null, style: "comic", view: "frame", current: null };

const $ = (sel) => document.querySelector(sel);
const drop = $("#drop"), photoInput = $("#photo"), preview = $("#preview"), dropText = $("#dropText");
const goBtn = $("#go"), pushBtn = $("#push"), statusEl = $("#status"), metaEl = $("#meta");
const viewEl = $("#view"), historyEl = $("#history");

function setStatus(text, kind = "") {
  statusEl.textContent = text;
  statusEl.className = `status ${kind}`;
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
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === state.view);
  });
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => { state.view = tab.dataset.view; showView(); });
});

function setCurrent(payload) {
  state.current = payload;
  state.view = "frame";
  showView();
  metaEl.textContent = payload
    ? `${payload.style_label} · ${payload.model} · $${payload.cost_usd} · ${payload.seconds}s · ${payload.width}×${payload.height}`
    : "";
  pushBtn.disabled = !(payload && payload.frame_url);
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
    setStatus("Remix ready. Check the E Ink simulation, then push it.", "ok");
  } catch (err) {
    setStatus(`⚠️ ${err.message}`, "bad");
  } finally {
    goBtn.disabled = false;
  }
});

pushBtn.addEventListener("click", async () => {
  if (!state.current) return;
  pushBtn.disabled = true;
  setStatus("📤 Uploading to the frame…", "busy");
  try {
    const resp = await fetch(`/api/push/${state.current.id}`, { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    setStatus(`✅ Pushed to ${data.device_id}. The panel refreshes in ~30 s.`, "ok");
    state.current.pushed_at = data.pushed_at;
    loadHistory();
  } catch (err) {
    setStatus(`⚠️ ${err.message}`, "bad");
  } finally {
    pushBtn.disabled = false;
  }
});

async function loadHistory() {
  try {
    const resp = await fetch("/api/history?limit=24");
    const items = await resp.json();
    historyEl.replaceChildren(...items.map((item) => {
      const card = document.createElement("button");
      card.className = "thumb";
      card.title = `${item.style_label} · ${new Date(item.created * 1000).toLocaleString()}`;
      const img = document.createElement("img");
      img.src = item.eink_url || item.frame_url || item.result_url || "";
      img.alt = item.style_label;
      img.loading = "lazy";
      const pushed = document.createElement("span");
      pushed.textContent = item.pushed_at ? "✅" : "";
      card.append(img, pushed);
      card.addEventListener("click", () => {
        state.current = item;
        state.view = "eink";
        showView();
        metaEl.textContent = `${item.style_label} · ${item.model} · $${item.cost_usd} · ${item.seconds}s`;
        pushBtn.disabled = !item.frame_url;
      });
      return card;
    }));
  } catch { /* gallery is best-effort */ }
}

loadHistory();
