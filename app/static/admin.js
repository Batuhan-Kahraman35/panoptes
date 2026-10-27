"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let state = { branches: [], lists: [], settings: {} };
let currentListId = null;
let draft = null;      // düzenlenen listenin kaydedilmemiş kopyası
let dirty = false;

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 2500);
}

async function refresh() {
  state = await api("GET", "/api/state");
  renderBranches();
  renderLists();
  renderSettings();
}

const DAYS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"];

function renderSettings() {
  const s = state.settings;
  const sch = s.schedule || { enabled: false, days: [], on: "08:30", off: "19:30" };
  $("#set-rotate").value = s.rotate_seconds;
  $("#sch-enabled").checked = sch.enabled;
  $("#sch-on").value = sch.on;
  $("#sch-off").value = sch.off;
  $("#sch-days").innerHTML = DAYS.map((d, i) =>
    `<label class="chip"><input type="checkbox" value="${i}" ${sch.days.includes(i) ? "checked" : ""}> ${d}</label>`).join("");
}

// Silme gibi işlemlerde tarayıcı confirm() yerine iki tıklamalı onay.
function confirmClick(btn, action) {
  if (btn.dataset.armed) return action();
  const original = btn.textContent;
  btn.dataset.armed = "1";
  btn.textContent = "Emin misiniz?";
  setTimeout(() => { delete btn.dataset.armed; btn.textContent = original; }, 3000);
}

// ------------------------------------------------------------------ sekmeler

document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x === b));
    for (const id of ["branches", "lists", "alarms", "settings"]) $(`#tab-${id}`).hidden = id !== b.dataset.tab;
    if (b.dataset.tab === "alarms") loadAlarms(true);
  })
);

// ------------------------------------------------------------------ şubeler

function statusBadge(b) {
  const t = b.last_test;
  if (!t) return `<span class="badge">Test edilmedi</span>`;
  if (t.ok && t.warnings.length) return `<span class="badge warn">Bağlı · ${t.warnings.length} uyarı</span>`;
  if (t.ok) return `<span class="badge ok">Bağlı</span>`;
  return `<span class="badge bad">Hata</span>`;
}

function codecCell(s) {
  if (!s) return `<span class="muted">—</span>`;
  const cls = s.codec === "H.264" ? "" : "warn";
  return `<span class="badge ${cls}">${esc(s.codec)}</span> <span class="muted">${esc(s.res)}${s.fps ? " · " + s.fps + " fps" : ""}</span>`;
}

const isDisabled = (b, no) => (b.disabled || []).includes(no);

function channelTable(b, withPreview) {
  if (!b.channels?.length) return `<p class="muted">Kanal bilgisi yok. Önce test edin.</p>`;
  return `<table><thead><tr><th>No</th><th>Kamera</th><th>Alt akış</th><th>Ana akış</th><th>Durum</th>${withPreview ? "<th>Kullanım</th><th></th>" : ""}</tr></thead><tbody>${b.channels
    .map((c) => {
      const off = isDisabled(b, c.no);
      return `<tr${off ? ' style="opacity:.55"' : ""}>
      <td>${c.no}</td><td>${esc(c.name)}</td><td>${codecCell(c.sub)}</td><td>${codecCell(c.main)}</td>
      <td>${c.online === false ? '<span class="badge bad">Çevrimdışı</span>' : c.online ? '<span class="badge ok">Çevrimiçi</span>' : '<span class="muted">—</span>'}</td>
      ${withPreview ? `<td><button class="small ${off ? "state-off" : "state-on"}" data-act="toggle" data-no="${c.no}" data-active="${off ? 1 : 0}"
          title="${off ? "Tıklayınca aktif olur: ekranda ve alarmlarda tekrar kullanılır" : "Tıklayınca pasif olur: ekranda boş kare kalır, alarm üretmez"}">${off ? "Pasif" : "Aktif"}</button></td>
        <td class="preview" data-no="${c.no}"><button class="small" data-act="preview" data-no="${c.no}">Önizle</button></td>` : ""}
    </tr>`;
    }).join("")}</tbody></table>`;
}

function renderBranches() {
  const el = $("#branch-list");
  if (!state.branches.length) {
    el.innerHTML = `<div class="empty">Henüz şube yok. <b>+ Yeni Şube</b> ile ilk DVR'ı ekleyin.</div>`;
    return;
  }
  el.innerHTML = state.branches.map((b) => {
    const dev = b.last_test?.device;
    const passive = (b.disabled || []).length;
    const info = [esc(b.ip), dev?.model && esc(dev.model), `${b.channels?.length || 0} kamera${passive ? ` (${passive} pasif)` : ""}`, b.last_test && `son test ${esc(b.last_test.time)}`].filter(Boolean).join(" · ");
    const nonH264 = (b.channels || []).filter((c) => c.sub && c.sub.codec !== "H.264").length;
    return `<div class="card" data-id="${b.id}">
      <div class="card-head">
        <h3>${esc(b.name)}</h3> ${statusBadge(b)}
        <span class="spacer"></span>
        <div class="actions">
          ${nonH264 ? `<button data-act="fix" title="DVR'deki alt akış kodlamasını H.264 yapar; kayıt (ana akış) değişmez">Alt akışları H.264 yap (${nonH264})</button>` : ""}
          <button data-act="test">Test Et</button>
          <button data-act="cams">Kameralar</button>
          <button data-act="edit">Düzenle</button>
          <button data-act="delete" class="danger">Sil</button>
        </div>
      </div>
      <div class="muted">${info}</div>
      <div class="cams" hidden></div>
    </div>`;
  }).join("");
}

$("#branch-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const card = btn.closest(".card");
  const b = state.branches.find((x) => x.id === card.dataset.id);
  switch (btn.dataset.act) {
    case "test":
      openTestDialog(b);
      break;
    case "fix":
      confirmClick(btn, () => openFixDialog(b));
      break;
    case "edit":
      openBranchDialog(b);
      break;
    case "delete":
      confirmClick(btn, async () => {
        await api("DELETE", `/api/branches/${b.id}`);
        toast(`${b.name} silindi`);
        draft = null;
        refresh();
      });
      break;
    case "cams": {
      const box = $(".cams", card);
      box.hidden = !box.hidden;
      if (!box.hidden) box.innerHTML = channelTable(b, true);
      break;
    }
    case "toggle": {
      const active = btn.dataset.active === "1";
      const updated = await api("PUT", `/api/branches/${b.id}/channels/${btn.dataset.no}`, { active });
      Object.assign(b, updated);
      $(".cams", card).innerHTML = channelTable(b, true);
      const name = b.channels.find((c) => c.no === Number(btn.dataset.no))?.name;
      toast(`${name} ${active ? "aktif" : "pasif"} yapıldı`);
      refresh().then(() => {
        // Yenilemede kart yeniden çizilir; açık olan kamera tablosunu geri aç
        const c = document.querySelector(`.card[data-id="${b.id}"]`);
        const box = c && $(".cams", c);
        if (box) { box.hidden = false; box.innerHTML = channelTable(state.branches.find((x) => x.id === b.id), true); }
      });
      break;
    }
    case "preview": {
      const td = btn.closest("td");
      td.innerHTML = `<span class="spinner"></span>`;
      const img = new Image();
      img.onload = () => td.replaceChildren(img);
      img.onerror = () => (td.innerHTML = `<span class="badge bad">Görüntü alınamadı</span>`);
      img.src = `/api/branches/${b.id}/snapshot/${btn.dataset.no}?t=${Date.now()}`;
      break;
    }
  }
});

// ------------------------------------------------------------------ dialog

const dlg = $("#dlg");

function showDialog(title, body, buttons) {
  $("#dlg-title").textContent = title;
  $("#dlg-body").innerHTML = body;
  const foot = $("#dlg-foot");
  foot.replaceChildren();
  for (const [label, cls, fn] of buttons) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    if (cls) btn.className = cls;
    btn.addEventListener("click", fn);
    foot.append(btn);
  }
  if (!dlg.open) dlg.showModal();
}

function openBranchDialog(b, error) {
  const v = b || {};
  showDialog(b ? `${b.name} — Düzenle` : "Yeni Şube", `
    ${error ? `<div class="error">${esc(error)}</div>` : ""}
    <label class="field"><span>Şube adı</span><input name="name" value="${esc(v.name)}" placeholder="Örn. Merkez Şube" required></label>
    <label class="field"><span>IP adresi veya alan adı</span><input name="ip" value="${esc(v.ip)}" placeholder="Örn. 85.105.10.20"></label>
    <div class="row2">
      <label class="field"><span>Kullanıcı adı</span><input name="username" value="${esc(v.username || "admin")}" autocomplete="off"></label>
      <label class="field"><span>Şifre</span><input name="password" type="password" autocomplete="new-password" placeholder="${b ? "Değiştirmek için yazın" : ""}"></label>
    </div>
    <label class="field"><span>Akış şifresi (Hik-Connect doğrulama kodu) — sadece DVR'da akış şifreleme açıksa</span>
      <input name="stream_key" type="password" autocomplete="off" placeholder="${v.has_stream_key ? "Kayıtlı — değiştirmek için yazın" : "Örn. ABCDEF"}"></label>
    <details ${v.http_port && (v.http_port !== 80 || v.rtsp_port !== 554) ? "open" : ""}>
      <summary>Gelişmiş: portlar</summary>
      <div class="row2">
        <label class="field"><span>HTTP portu (cihaz ayarları)</span><input name="http_port" type="number" value="${v.http_port || 80}"></label>
        <label class="field"><span>RTSP portu (görüntü)</span><input name="rtsp_port" type="number" value="${v.rtsp_port || 554}"></label>
      </div>
    </details>`,
    [["İptal", "", () => dlg.close()], ["Kaydet ve Test Et", "primary", () => saveBranch(b)]]);
  $("#dlg-body input[name=name]").focus();
}

async function saveBranch(b) {
  const form = {};
  for (const inp of $("#dlg-body").querySelectorAll("input")) form[inp.name] = inp.value;
  try {
    const saved = b ? await api("PUT", `/api/branches/${b.id}`, form) : await api("POST", "/api/branches", form);
    await refresh();
    openTestDialog(saved);
  } catch (err) {
    openBranchDialog(b ? { ...b, ...form } : form, err.message);
    if (!b) $("#dlg-body input[name=password]").value = form.password;
  }
}

async function openTestDialog(b) {
  showDialog(`${b.name} — Test`, `<p><span class="spinner"></span>DVR'a bağlanılıyor ve kanallar okunuyor…</p>`,
    [["Kapat", "", () => dlg.close()]]);
  let r;
  try {
    r = await api("POST", `/api/branches/${b.id}/test`);
  } catch (err) {
    $("#dlg-body").innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  await refresh();
  const fresh = r.branch || b;
  $("#dlg-body").innerHTML = `
    <p>${r.ok ? '<span class="badge ok">Bağlantı başarılı</span>' : '<span class="badge bad">Bağlantı sorunlu</span>'}
       <span class="muted"> ${esc(r.time)}</span></p>
    <ul class="steps">${r.steps.map((s) => `<li class="${s.ok ? "ok" : "bad"}"><b>${esc(s.name)}</b> — ${esc(s.detail)}</li>`).join("")}</ul>
    ${r.warnings.length ? `<ul class="warnings">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
    ${r.channels.length ? channelTable(fresh, false) : ""}`;
  showDialog(`${b.name} — Test`, $("#dlg-body").innerHTML, [
    ["Düzenle", "", () => openBranchDialog(fresh)],
    ["Tekrar Test Et", "", () => openTestDialog(fresh)],
    ["Kapat", "primary", () => dlg.close()],
  ]);
}

async function openFixDialog(b) {
  showDialog(`${b.name} — Alt akış H.264`, `<p><span class="spinner"></span>DVR'da alt akış kodlaması değiştiriliyor…</p>`,
    [["Kapat", "", () => dlg.close()]]);
  let r;
  try {
    r = await api("POST", `/api/branches/${b.id}/fix-substream`);
  } catch (err) {
    $("#dlg-body").innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  await refresh();
  const fresh = r.test.branch || b;
  $("#dlg-body").innerHTML = `
    <ul class="steps">${r.results.map((x) => `<li class="${x.ok ? "ok" : "bad"}"><b>${esc(x.name)}</b> — ${esc(x.detail)}</li>`).join("") || "<li>Değiştirilecek kanal yok.</li>"}</ul>
    <p class="muted">Güncel durum:</p>
    ${channelTable(fresh, false)}`;
}

$("#btn-new-branch").addEventListener("click", () => openBranchDialog(null));

// ------------------------------------------------------------------ listeler

function gridDims(grid, n) {
  if (grid === "auto") {
    const cols = Math.max(1, Math.ceil(Math.sqrt(n)));
    return [cols, Math.max(1, Math.ceil(n / cols))];
  }
  const [c, r] = grid.split("x").map(Number);
  return [c, r];
}

function camInfo(item) {
  const b = state.branches.find((x) => x.id === item.branch);
  const c = b?.channels?.find((x) => x.no === item.channel);
  return { branch: b?.name ?? "Silinmiş şube", cam: (c?.name ?? `Kanal ${item.channel}`) + (b && isDisabled(b, item.channel) ? " (pasif)" : ""), missing: !c };
}

function selectList(id) {
  if (dirty && !confirmDiscard()) return;
  currentListId = id;
  const l = state.lists.find((x) => x.id === id);
  draft = l ? structuredClone(l) : null;
  dirty = false;
  renderLists();
}

function confirmDiscard() {
  const ok = $("#list-editor .unsaved");
  if (ok && !ok.dataset.armed) {
    ok.dataset.armed = "1";
    ok.textContent = "Kaydedilmemiş değişiklikler var — çıkmak için tekrar tıklayın";
    return false;
  }
  return true;
}

function renderLists() {
  if (currentListId && !state.lists.some((l) => l.id === currentListId)) currentListId = null;
  if (!currentListId && state.lists.length) currentListId = state.lists[0].id;
  if (!draft || draft.id !== currentListId) {
    const l = state.lists.find((x) => x.id === currentListId);
    draft = l ? structuredClone(l) : null;
    dirty = false;
  }

  $("#list-nav").innerHTML = state.lists.map((l, i) => `
    <div class="item ${l.id === currentListId ? "active" : ""}" data-id="${l.id}">
      <span class="name">${esc(l.name)}</span>
      ${l.active ? "" : '<span class="badge">pasif</span>'}
      <button class="small" data-move="up" ${i === 0 ? "disabled" : ""} title="Yukarı">↑</button>
      <button class="small" data-move="down" ${i === state.lists.length - 1 ? "disabled" : ""} title="Aşağı">↓</button>
    </div>`).join("") || `<p class="muted">Liste yok.</p>`;

  renderEditor();
}

function renderEditor() {
  const ed = $("#list-editor");
  if (!draft) {
    ed.innerHTML = `<div class="empty">Ekranda gösterilecek kameraları seçmek için bir liste oluşturun.</div>`;
    return;
  }
  const filled = draft.items.filter((x) => !x.empty).length;
  const [cols, rows] = gridDims(draft.grid, Math.max(draft.items.length, 1));
  const cap = cols * rows;
  const shown = Math.max(cap, draft.items.length);
  const used = new Set(draft.items.filter((x) => !x.empty).map((x) => `${x.branch}:${x.channel}`));

  let cells = "";
  for (let i = 0; i < shown; i++) {
    const it = draft.items[i];
    const over = i >= cap ? " overflow" : "";
    if (!it || it.empty) {
      cells += `<div class="cell${over}" data-i="${i}"><span class="num">${i + 1}</span><span class="br">boş</span></div>`;
    } else {
      const info = camInfo(it);
      cells += `<div class="cell filled${over}" draggable="true" data-i="${i}">
        <span class="num">${i + 1}</span>
        <span class="lbl">${esc(info.cam)}${info.missing ? " ⚠" : ""}</span>
        <span class="br">${esc(info.branch)}</span>
        <span class="ops"><button class="small" data-op="clear" title="Hücreyi boşalt">✕</button></span>
      </div>`;
    }
  }

  const palette = state.branches.map((b) => `
    <details ${state.branches.length <= 3 ? "open" : ""}>
      <summary>${esc(b.name)} <span class="muted">(${b.channels?.length || 0})</span></summary>
      ${b.channels?.length ? `<div class="chips">${b.channels.map((c) => {
        const h265 = c.sub && c.sub.codec !== "H.264";
        const off = isDisabled(b, c.no);
        return `<button class="chip ${h265 ? "h265" : ""}" data-add="${b.id}:${c.no}" ${used.has(`${b.id}:${c.no}`) || off ? "disabled" : ""}
          title="${off ? "Pasif kamera — Şubeler > Kameralar'dan aktif yapın" : h265 ? "Alt akış " + esc(c.sub.codec) + " — H.264 yapılmalı" : ""}">${esc(c.name)}${off ? " (pasif)" : ""}</button>`;
      }).join("")}<button class="chip" data-addall="${b.id}">+ Tümü</button></div>` : `<p class="muted">Kanal yok — şubeyi test edin.</p>`}
    </details>`).join("");

  ed.innerHTML = `
    <div class="section">
      <div class="inline">
        <label class="field" style="flex:1"><span>Liste adı</span><input id="l-name" value="${esc(draft.name)}"></label>
        <label class="field"><span>Düzen</span><select id="l-grid">
          ${["auto", "1x1", "2x2", "3x3", "4x4", "5x5"].map((g) => `<option value="${g}" ${draft.grid === g ? "selected" : ""}>${g === "auto" ? "Otomatik" : g}</option>`).join("")}
        </select></label>
        <label class="field"><span>Kalite</span><select id="l-quality">
          <option value="sub" ${draft.quality !== "main" ? "selected" : ""}>Alt akış (önerilen)</option>
          <option value="main" ${draft.quality === "main" ? "selected" : ""}>Ana akış</option>
        </select></label>
        <label class="field" style="min-width:auto"><span>&nbsp;</span><label><input type="checkbox" id="l-active" ${draft.active ? "checked" : ""}> Aktif</label></label>
      </div>
      <p class="muted" style="margin:12px 0 0">${filled} kamera · ${cols}×${rows} ekran. Hücreleri sürükleyerek yerini değiştirin, boş hücreler ekranda boş kalır.
        ${draft.items.length > cap ? '<b style="color:var(--bad)">Kesik çizgili hücreler ekrana sığmıyor.</b>' : ""}</p>
      <div class="grid-preview" id="grid-preview" style="grid-template-columns:repeat(${cols},1fr)">${cells}</div>
      <div class="actions" style="margin-top:12px">
        <button class="primary" id="l-save" ${dirty ? "" : "disabled"}>Kaydet</button>
        <button id="l-addempty">+ Boş hücre</button>
        <button id="l-compact">Boşlukları kaldır</button>
        <span class="spacer" style="flex:1"></span>
        <button class="danger" id="l-delete">Listeyi sil</button>
      </div>
      ${dirty ? '<p class="unsaved" style="color:var(--warn)">Kaydedilmemiş değişiklikler var.</p>' : ""}
    </div>
    <div class="palette"><h2 style="margin-bottom:8px">Kamera ekle</h2>${palette || '<p class="muted">Önce şube ekleyin.</p>'}</div>`;
}

function changed() {
  dirty = true;
  renderEditor();
}

function placeItem(item) {
  const [cols, rows] = gridDims(draft.grid, Math.max(draft.items.length, 1));
  const cap = draft.grid === "auto" ? Infinity : cols * rows;
  const free = draft.items.findIndex((x, i) => x.empty && i < cap);
  if (free >= 0) draft.items[free] = item;
  else draft.items.push(item);
}

$("#list-nav").addEventListener("click", async (e) => {
  const row = e.target.closest(".item");
  if (!row) return;
  const move = e.target.closest("button[data-move]");
  if (move) {
    await api("POST", `/api/lists/${row.dataset.id}/move`, { direction: move.dataset.move });
    return refresh();
  }
  if (row.dataset.id !== currentListId) selectList(row.dataset.id);
});

$("#btn-new-list").addEventListener("click", async () => {
  if (dirty && !confirmDiscard()) return;
  const l = await api("POST", "/api/lists", {});
  currentListId = l.id;
  draft = null;
  await refresh();
});

const editor = $("#list-editor");

editor.addEventListener("input", (e) => {
  if (!draft) return;
  if (e.target.id === "l-name") { draft.name = e.target.value; dirty = true; $("#l-save").disabled = false; }
});

editor.addEventListener("change", (e) => {
  if (!draft) return;
  if (e.target.id === "l-grid") { draft.grid = e.target.value; changed(); }
  if (e.target.id === "l-quality") { draft.quality = e.target.value; changed(); }
  if (e.target.id === "l-active") { draft.active = e.target.checked; changed(); }
});

editor.addEventListener("click", async (e) => {
  if (!draft) return;
  const t = e.target;
  if (t.dataset.add) {
    const [branch, ch] = t.dataset.add.split(":");
    placeItem({ branch, channel: Number(ch) });
    return changed();
  }
  if (t.dataset.addall) {
    const b = state.branches.find((x) => x.id === t.dataset.addall);
    const used = new Set(draft.items.filter((x) => !x.empty).map((x) => `${x.branch}:${x.channel}`));
    for (const c of b.channels || []) if (!used.has(`${b.id}:${c.no}`) && !isDisabled(b, c.no)) placeItem({ branch: b.id, channel: c.no });
    return changed();
  }
  if (t.dataset.op === "clear") {
    const i = Number(t.closest(".cell").dataset.i);
    draft.items[i] = { empty: true };
    return changed();
  }
  switch (t.id) {
    case "l-addempty":
      draft.items.push({ empty: true });
      return changed();
    case "l-compact":
      draft.items = draft.items.filter((x) => !x.empty);
      return changed();
    case "l-save": {
      while (draft.items.length && draft.items.at(-1).empty) draft.items.pop();
      try {
        const saved = await api("PUT", `/api/lists/${draft.id}`, draft);
        draft = structuredClone(saved);
        dirty = false;
        toast("Liste kaydedildi — ekran birkaç saniye içinde güncellenir");
        await refresh();
      } catch (err) {
        toast(err.message);
      }
      return;
    }
    case "l-delete":
      return confirmClick(t, async () => {
        await api("DELETE", `/api/lists/${draft.id}`);
        draft = null;
        dirty = false;
        currentListId = null;
        refresh();
      });
  }
});

// Sürükle-bırak: hücreler yer değiştirir (boş hücreye bırakınca taşınır).
let dragFrom = null;
editor.addEventListener("dragstart", (e) => {
  const cell = e.target.closest?.(".cell");
  if (!cell) return;
  dragFrom = Number(cell.dataset.i);
  cell.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
});
editor.addEventListener("dragend", () => {
  dragFrom = null;
  editor.querySelectorAll(".cell").forEach((c) => c.classList.remove("dragging", "over"));
});
editor.addEventListener("dragover", (e) => {
  const cell = e.target.closest(".cell");
  if (cell && dragFrom !== null) {
    e.preventDefault();
    editor.querySelectorAll(".cell.over").forEach((c) => c !== cell && c.classList.remove("over"));
    cell.classList.add("over");
  }
});
editor.addEventListener("drop", (e) => {
  const cell = e.target.closest(".cell");
  if (!cell || dragFrom === null) return;
  e.preventDefault();
  const to = Number(cell.dataset.i);
  const items = draft.items;
  while (items.length <= Math.max(to, dragFrom)) items.push({ empty: true });
  [items[dragFrom], items[to]] = [items[to], items[dragFrom]];
  dragFrom = null;
  changed();
});

// ------------------------------------------------------------------ ayarlar

async function saveSettings() {
  try {
    await api("PUT", "/api/settings", {
      rotate_seconds: Number($("#set-rotate").value),
      schedule: {
        enabled: $("#sch-enabled").checked,
        days: [...document.querySelectorAll("#sch-days input:checked")].map((x) => Number(x.value)),
        on: $("#sch-on").value,
        off: $("#sch-off").value,
      },
    });
    toast("Ayarlar kaydedildi");
    refresh();
  } catch (err) {
    toast(err.message);
  }
}

$("#btn-save-settings").addEventListener("click", saveSettings);
$("#btn-save-schedule").addEventListener("click", saveSettings);

// ------------------------------------------------------------------ alarmlar

async function loadAlarms(fillForm) {
  const a = await api("GET", "/api/alarms");
  const open = a.active.filter((x) => !x.acked).length;
  $("#alarm-count").textContent = open;
  $("#alarm-count").hidden = !open;
  $("#alarm-last").textContent = a.running ? "kontrol ediliyor…" : a.last_run ? `son kontrol ${a.last_run}` : "henüz kontrol edilmedi";
  $("#alarm-active").innerHTML = a.active.length
    ? a.active.map((x) => `<div class="card" data-key="${esc(x.key)}"><div class="card-head">
        <span class="badge ${x.acked ? "" : "bad"}">${x.acked ? "Görüldü" : "Alarm"}</span>
        <span>${esc(x.text)}</span><span class="spacer"></span>
        <span class="muted">${esc(x.since)}'den beri</span>
        ${x.acked ? "" : `<button class="small" data-ack="${esc(x.key)}">Gördüm</button>`}
      </div></div>`).join("")
    : `<div class="empty">Açık alarm yok.</div>`;
  $("#alarm-history").innerHTML = a.history.map((h) =>
    `<tr><td>${esc(h.time)}</td><td><span class="badge ${h.event === "alarm" ? "bad" : "ok"}">${esc(h.event)}</span></td><td>${esc(h.text)}</td></tr>`).join("")
    || `<tr><td colspan="3" class="muted">Kayıt yok</td></tr>`;
  if (fillForm) {
    const s = a.settings, e = s.email;
    $("#em-enabled").checked = e.enabled;
    $("#em-host").value = e.host;
    $("#em-port").value = e.port;
    $("#em-security").value = e.security;
    $("#em-username").value = e.username;
    $("#em-password").value = "";
    $("#em-password").placeholder = e.has_password ? "Kayıtlı — değiştirmek için yazın" : "";
    $("#em-sender").value = e.sender;
    $("#em-to").value = e.to.join(", ");
    $("#al-interval").value = s.interval_min;
    $("#al-gap").value = s.record_gap_min;
  }
}

$("#alarm-active").addEventListener("click", async (e) => {
  const key = e.target.dataset.ack;
  if (!key) return;
  await api("POST", "/api/alarms/ack", { key });
  loadAlarms(false);
});

$("#btn-check-now").addEventListener("click", async () => {
  await api("POST", "/api/alarms/check");
  toast("Kontrol başlatıldı; birkaç dakika sürebilir");
  setTimeout(() => loadAlarms(false), 3000);
});

$("#btn-save-alarms").addEventListener("click", async () => {
  try {
    await api("PUT", "/api/alarms/settings", {
      interval_min: Number($("#al-interval").value),
      record_gap_min: Number($("#al-gap").value),
      email: {
        enabled: $("#em-enabled").checked, host: $("#em-host").value, port: Number($("#em-port").value),
        security: $("#em-security").value, username: $("#em-username").value, password: $("#em-password").value,
        sender: $("#em-sender").value, to: $("#em-to").value,
      },
    });
    toast("Alarm ayarları kaydedildi");
    loadAlarms(true);
  } catch (err) {
    toast(err.message);
  }
});

$("#btn-test-email").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try {
    await api("POST", "/api/alarms/test-email");
    toast("Test e-postası gönderildi");
  } catch (err) {
    toast(err.message);
  } finally {
    e.target.disabled = false;
  }
});

loadAlarms(false).catch(() => {});
setInterval(() => loadAlarms(false).catch(() => {}), 30000);

refresh().catch((err) => {
  $("#branch-list").innerHTML = `<div class="error">Sunucuya bağlanılamadı: ${esc(err.message)}</div>`;
});
