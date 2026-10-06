/* 陶瓷合窑装载确认台 — 原生 JavaScript */
"use strict";

const $ = (sel) => document.querySelector(sel);

let batches = [];
let kilns = [];
let loads = [];
let currentKilnId = null;
let draft = null;    // 当前窑的草案 {id, kiln_id, version, items:[...]}
let preview = null;  // 最近一次预览结果（确认时携带其中的草案/批次版本）
let editingBatchId = null;

/* ---------- 基础 ---------- */

async function api(path, method = "GET", body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON 响应 */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || ("请求失败 HTTP " + res.status));
    err.status = res.status;
    err.payload = data;
    throw err;
  }
  return data;
}

function showMsg(text, ok = false) {
  const el = $("#msg");
  el.textContent = text;
  el.className = "msg " + (ok ? "ok" : "err");
  el.style.display = "block";
  clearTimeout(showMsg._t);
  showMsg._t = setTimeout(() => { el.style.display = "none"; }, 9000);
}

const fmtRange = (lo, hi, unit) => `[${lo}, ${hi}] ${unit}`;

/* ---------- 批次 ---------- */

function renderBatches() {
  const tbody = $("#batch-table tbody");
  tbody.innerHTML = "";
  for (const b of batches) {
    const tr = document.createElement("tr");
    const used = b.used_by
      ? `<span class="tag used">已用于 ${b.used_by}</span>`
      : `<span class="tag free">可用</span>`;
    tr.innerHTML = `
      <td>${b.batch_no}</td>
      <td>${fmtRange(b.peak_min, b.peak_max, "℃")}</td>
      <td>${fmtRange(b.soak_min, b.soak_max, "分")}</td>
      <td>v${b.version}</td>
      <td>${used}</td>
      <td class="ops"></td>`;
    const ops = tr.querySelector(".ops");
    const editBtn = document.createElement("button");
    editBtn.textContent = "修改";
    editBtn.className = "ghost small";
    editBtn.disabled = !!b.used_by;
    editBtn.onclick = () => startEditBatch(b);
    const delBtn = document.createElement("button");
    delBtn.textContent = "删除";
    delBtn.className = "ghost small danger";
    delBtn.disabled = !!b.used_by;
    delBtn.onclick = () => deleteBatch(b);
    ops.append(editBtn, delBtn);
    tbody.appendChild(tr);
  }
}

function startEditBatch(b) {
  editingBatchId = b.id;
  $("#b-no").value = b.batch_no;
  $("#b-peak-min").value = b.peak_min;
  $("#b-peak-max").value = b.peak_max;
  $("#b-soak-min").value = b.soak_min;
  $("#b-soak-max").value = b.soak_max;
  $("#batch-submit").textContent = "保存修改（版本+1）";
  $("#batch-cancel").style.display = "";
}

function resetBatchForm() {
  editingBatchId = null;
  $("#batch-form").reset();
  $("#batch-submit").textContent = "登记批次";
  $("#batch-cancel").style.display = "none";
}

async function submitBatch(e) {
  e.preventDefault();
  const payload = {
    batch_no: $("#b-no").value.trim(),
    peak_min: $("#b-peak-min").value,
    peak_max: $("#b-peak-max").value,
    soak_min: $("#b-soak-min").value,
    soak_max: $("#b-soak-max").value,
  };
  try {
    if (editingBatchId) {
      await api(`/api/batches/${editingBatchId}`, "PUT", payload);
      showMsg("批次已更新（版本+1）。包含该批次的旧预览已失效，需重新预览。", true);
    } else {
      await api("/api/batches", "POST", payload);
      showMsg(`批次 ${payload.batch_no} 已登记`, true);
    }
    resetBatchForm();
    await refreshAll();
  } catch (err) {
    showMsg(err.message);
  }
}

async function deleteBatch(b) {
  if (!confirm(`确定删除批次 ${b.batch_no}？`)) return;
  try {
    await api(`/api/batches/${b.id}`, "DELETE");
    showMsg(`批次 ${b.batch_no} 已删除`, true);
    await refreshAll();
  } catch (err) {
    showMsg(err.message);
  }
}

/* ---------- 窑与草案 ---------- */

function renderKilnSelect() {
  const sel = $("#kiln-select");
  sel.innerHTML = "";
  for (const k of kilns) {
    const opt = document.createElement("option");
    opt.value = k.id;
    opt.textContent = `窑 ${k.code}（${k.position_count} 位）`;
    sel.appendChild(opt);
  }
  if (currentKilnId && kilns.some((k) => k.id === currentKilnId)) {
    sel.value = currentKilnId;
  } else if (kilns.length) {
    currentKilnId = kilns[0].id;
    sel.value = currentKilnId;
  } else {
    currentKilnId = null;
  }
}

function currentKiln() {
  return kilns.find((k) => k.id === currentKilnId) || null;
}

async function loadDraft() {
  if (!currentKilnId) {
    draft = null; preview = null;
    renderBoard(); renderPreview();
    return;
  }
  draft = await api(`/api/kilns/${currentKilnId}/draft`);
  preview = null;
  renderBoard();
  renderPreview();
}

function renderBoard() {
  const board = $("#board");
  board.innerHTML = "";
  const kiln = currentKiln();
  if (!kiln || !draft) {
    board.innerHTML = '<p class="hint">请先添加一台窑。</p>';
    return;
  }
  const byPos = {};
  for (const it of draft.items) byPos[it.position_no] = it;
  const placedBatchIds = new Set(draft.items.map((it) => it.batch_id));

  for (let n = 1; n <= kiln.position_count; n++) {
    const occupant = byPos[n] || null;
    const cell = document.createElement("div");
    cell.className = "cell" + (occupant ? " filled" : "");

    const label = document.createElement("div");
    label.className = "cell-no";
    label.textContent = `窑位 ${n}`;

    const sel = document.createElement("select");
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "（空）";
    sel.appendChild(empty);
    for (const b of batches) {
      const isOccupant = occupant && occupant.batch_id === b.id;
      if (!isOccupant) {
        if (b.used_by) continue;              // 已被确认窑次使用
        if (placedBatchIds.has(b.id)) continue; // 已占本草案其他窑位
      }
      const opt = document.createElement("option");
      opt.value = b.id;
      opt.textContent = `${b.batch_no} v${b.version}`;
      sel.appendChild(opt);
    }
    if (occupant) sel.value = String(occupant.batch_id);
    sel.onchange = () => assignPosition(n, sel.value ? Number(sel.value) : null);

    cell.append(label, sel);
    if (occupant) {
      const info = document.createElement("div");
      info.className = "cell-info";
      info.textContent = `${occupant.peak_min}~${occupant.peak_max}℃ · ${occupant.soak_min}~${occupant.soak_max}分`;
      cell.appendChild(info);
    }
    board.appendChild(cell);
  }
}

async function assignPosition(positionNo, batchId) {
  try {
    if (batchId) {
      draft = await api(`/api/drafts/${draft.id}/items/${positionNo}`, "PUT", { batch_id: batchId });
    } else {
      draft = await api(`/api/drafts/${draft.id}/items/${positionNo}`, "DELETE");
    }
    preview = null;
    renderBoard();
    renderPreview();
    await doPreview(); // 变更后自动刷新预览，确认前始终持有最新版本
  } catch (err) {
    showMsg(err.message);
    await refreshAll();
  }
}

/* ---------- 预览与确认 ---------- */

function renderPreview() {
  const body = $("#preview-body");
  const btn = $("#btn-confirm");
  if (!preview) {
    body.innerHTML = '<p class="hint">把批次放入窑位后，点击「刷新预览」查看共同工艺区间。</p>';
    btn.disabled = true;
    return;
  }
  let html = `<div class="preview-meta">窑 ${preview.kiln.code} · 草案版本 v${preview.draft_version} · 占用 ${preview.items.length}/${preview.kiln.position_count} 位</div>`;
  if (preview.common_peak) {
    html += `<div class="common ok-line">共同峰温区间：<b>${fmtRange(preview.common_peak[0], preview.common_peak[1], "℃")}</b></div>`;
  } else {
    html += `<div class="common bad-line">共同峰温区间：无交集</div>`;
  }
  if (preview.common_soak) {
    html += `<div class="common ok-line">共同保温区间：<b>${fmtRange(preview.common_soak[0], preview.common_soak[1], "分钟")}</b></div>`;
  } else {
    html += `<div class="common bad-line">共同保温区间：无交集</div>`;
  }
  html += '<div class="occ">';
  for (const o of preview.occupancy) {
    const cls = o.batch_no ? "occ-cell filled" : "occ-cell";
    const tip = o.batch_no ? `窑位 ${o.position_no}：${o.batch_no}` : `窑位 ${o.position_no}：空`;
    html += `<span class="${cls}" title="${tip}">${o.position_no}</span>`;
  }
  html += "</div>";
  if (preview.problems && preview.problems.length) {
    html += '<ul class="problems">' + preview.problems.map((p) => `<li>${p}</li>`).join("") + "</ul>";
  }
  body.innerHTML = html;
  btn.disabled = false; // 是否可确认以后端校验为准，问题会在 409 中指出
}

async function doPreview() {
  if (!draft) return;
  try {
    preview = await api(`/api/drafts/${draft.id}/preview`, "POST");
    renderPreview();
  } catch (err) {
    showMsg(err.message);
  }
}

async function doConfirm() {
  if (!draft || !preview) {
    showMsg("请先刷新预览，再确认装窑");
    return;
  }
  try {
    // 确认必须携带预览时的草案版本与各批次版本
    const load = await api(`/api/drafts/${draft.id}/confirm`, "POST", {
      draft_version: preview.draft_version,
      batch_versions: preview.batch_versions,
    });
    showMsg(`窑次 ${load.run_no} 已确认：装载清单与共同工艺区间已锁定保存`, true);
    preview = null;
    await refreshAll();
  } catch (err) {
    if (err.status === 409) {
      showMsg(err.message + "（已自动刷新预览）");
      await refreshAll();
      await doPreview();
    } else {
      showMsg(err.message);
    }
  }
}

/* ---------- 已确认窑次（快照） ---------- */

function renderLoads() {
  const box = $("#load-list");
  box.innerHTML = "";
  if (!loads.length) {
    box.innerHTML = '<p class="hint">暂无已确认窑次。</p>';
    return;
  }
  for (const l of loads) {
    const card = document.createElement("div");
    card.className = "load-card";
    const rows = l.items.map((it) => `
      <tr>
        <td>${it.position_no}</td>
        <td>${it.batch_no}</td>
        <td>${fmtRange(it.peak_min, it.peak_max, "℃")}</td>
        <td>${fmtRange(it.soak_min, it.soak_max, "分")}</td>
      </tr>`).join("");
    card.innerHTML = `
      <div class="load-head"><b>${l.run_no}</b> · 窑 ${l.kiln_code} · ${l.created_at}</div>
      <div class="load-common">
        共同峰温 <b>${fmtRange(l.peak_min, l.peak_max, "℃")}</b> ·
        共同保温 <b>${fmtRange(l.soak_min, l.soak_max, "分钟")}</b>
      </div>
      <table class="data-table small">
        <thead><tr><th>窑位</th><th>批次(快照)</th><th>峰温区间</th><th>保温区间</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    box.appendChild(card);
  }
}

/* ---------- 总刷新 ---------- */

async function refreshAll() {
  [batches, kilns, loads] = await Promise.all([
    api("/api/batches"), api("/api/kilns"), api("/api/loads"),
  ]);
  renderBatches();
  renderKilnSelect();
  renderLoads();
  await loadDraft();
}

/* ---------- 事件绑定与启动 ---------- */

$("#batch-form").addEventListener("submit", submitBatch);
$("#batch-cancel").addEventListener("click", resetBatchForm);
$("#kiln-select").addEventListener("change", async (e) => {
  currentKilnId = Number(e.target.value);
  await loadDraft();
});
$("#kiln-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const k = await api("/api/kilns", "POST", {
      code: $("#k-code").value.trim(),
      position_count: Number($("#k-count").value),
    });
    $("#kiln-form").reset();
    showMsg(`窑 ${k.code} 已添加`, true);
    currentKilnId = k.id;
    await refreshAll();
  } catch (err) {
    showMsg(err.message);
  }
});
$("#btn-preview").addEventListener("click", doPreview);
$("#btn-confirm").addEventListener("click", doConfirm);

refreshAll().catch((err) => showMsg(err.message));
