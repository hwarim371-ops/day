"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const icon = (name) => `<i data-lucide="${name}"></i>`;
const token = $("meta[name=booking-token]").content;
let data = null,
  view = "summary",
  selected = new Set(),
  activeKey = "",
  inspectorTab = "rooms",
  toastTimer,
  drawerDirty = false;
let drawerRevision = "";
const reviewSelected = new Set(), reviewDrafts = new Map();
let reviewFilter = "all", pendingReview = null;
let pendingAdd = null, pendingSave = null;
let activeTrend = null;
let businessSignature = "";
let tableSignature = "",
  proposalSignature = "",
  discoverySignature = "",
  jobsSignature = "",
  requesting = false;
const titles = {
  summary: "가동률 · 매출",
  catalog: "통합 관리",
  changes: "변경 점검",
  discovery: "지역 검색",
  history: "수집 기록",
};
const kinds = {
  "approve-review": "변경 확인 · DB 반영 및 재수집",
  collect: "예약현황 수집",
  scan: "객실명 점검",
  discover: "지역 업체 검색",
  verify: "업체 링크 확인",
  apply: "객실 DB 수정",
  archive: "목록 삭제 / 복원",
  "room-rules": "상품 판별 저장",
};
const states = {
  running: "작업 중",
  queued: "실행 대기",
  completed: "완료",
  issues: "확인 필요",
  failed: "실패",
  stopped: "중지",
  interrupted: "중단",
  idle: "대기",
};
function icons() {
  window.lucide?.createIcons();
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").className = "toast" + (error ? " error" : "");
  toastTimer = setTimeout(
    () => $("#toast").classList.add("hidden"),
    error ? 10000 : 4500,
  );
}
async function api(path, body) {
  if (window.bookingCloudApi) return window.bookingCloudApi(path, body);
  const r = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Booking-Token": token,
          },
          body: JSON.stringify(body),
        },
  );
  const v = await r.json();
  if (!r.ok) throw new Error(v.error || "요청 실패");
  return v;
}
function guarded(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (e) {
      toast(e.message, true);
    }
  };
}
function current() {
  return data?.entries.find((e) => e.key === activeKey);
}
function normal(e) {
  return (
    e.result?.status === "정상" &&
    !e.result.stale &&
    e.result.date === $("#checkin").value &&
    !e.changed && !e.proposal?.changed
  );
}
function status(e) {
  if (e.archived) return ["삭제됨", ""];
  if (pendingCollection(e)) return ["저장 완료 · 재수집 대기", "warn"];
  if (e.result?.status.startsWith("오류")) return ["수집 접속 오류", "error"];
  if (e.result?.missing > 0 && !e.result.superseded) return [`객실 ${e.result.missing}개 미확인`, "warn"];
  if (e.proposal?.changed)
    return [e.proposal.error ? "점검 오류" : "변경 후보", "warn"];
  if (!e.result) return ["미수집", ""];
  if (e.result.status.startsWith("오류")) return ["접속 오류", "error"];
  if (e.result.status !== "정상") return ["확인 필요", "warn"];
  if (e.result.legacy) return ["이전 기록", ""];
  if (e.result.stale) return ["DB 재확인", "warn"];
  if (e.result.date !== $("#checkin").value) return ["이전 날짜", ""];
  return ["정상", "ok"];
}
function pendingCollection(e) {
  return e.pending_collection ?? (!!e.changed && (!e.result || !!e.result.superseded));
}
function needsAttention(e) {
  return (
    e.changed ||
    e.proposal?.changed ||
    (e.result &&
      (e.result.status !== "정상" || (e.result.stale && !e.result.legacy)))
  );
}
function badge(e) {
  const [text, cls] = status(e);
  return `<span class="badge ${cls}" title="${esc(e.result?.status || "수집 기록 없음")}">${text}</span>`;
}
function visibleEntries() {
  const query = $("#search").value.trim().toLowerCase(),
    filter = $("#statusFilter").value;
  return data.entries
    .filter((e) => (filter === "archived" ? e.archived : !e.archived))
    .filter((e) =>
      filter === "issue"
        ? needsAttention(e)
        : filter === "changed"
          ? e.changed
          : filter === "normal"
            ? normal(e)
            : true,
    )
    .filter((e) =>
      [e.major, e.sheet_title, e.company_id, e.region, ...e.rooms]
        .join(" ")
        .toLowerCase()
        .includes(query),
    );
}
function empty(title, name = "inbox") {
  return `<div class="empty-state">${icon(name)}<strong>${esc(title)}</strong></div>`;
}
function formatTime(value) {
  return value
    ? value.slice(5, 10).replace("-", ".") + " " + value.slice(11, 16)
    : "-";
}
function money(v) {
  return v === null || v === undefined
    ? "-"
    : Number(v).toLocaleString("ko-KR") + "원";
}
function showView(next) {
  view = next;
  $("#pageTitle").textContent = titles[next];
  $$("nav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === next),
  );
  $$(".view").forEach((el) =>
    el.classList.toggle("hidden", el.id !== next + "View"),
  );
  render();
  if (next === "history" && data?.cloud) {
    api("/api/jobs").then(result => {
      data.jobs = result.jobs;
      renderJobs(); icons();
    }).catch(error => toast(error.message, true));
  }
}
async function refresh(force = false) {
  if (requesting) {
    await requesting.catch(() => {});
    if (!force) return;
  }
  const request = api("/api/state");
  requesting = request;
  try {
    const previousEntry = data ? JSON.stringify(current()) : null;
    data = await request;
    if (pendingSave && data.job.id === pendingSave.id && !data.busy) {
      const saved = pendingSave;
      pendingSave = null;
      if (["completed", "issues"].includes(data.job.state)) {
        const job = await api('/api/job', {id: saved.id});
        activeKey = job.response?.keys?.[0] || saved.key;
        drawerDirty = false;
        renderInspector();
        toast(data.job.state === "completed" ? "저장 및 수집 완료" : "수정은 저장됐습니다. 수집 확인이 필요한 항목이 있습니다.");
      } else {
        toast("저장 작업이 완료되지 않았습니다. 입력 내용은 유지됩니다. 활동 로그를 확인해 주세요.", true);
      }
    }
    if (pendingAdd && data.job.id === pendingAdd.id && !data.busy) {
      const pending = pendingAdd;
      pendingAdd = null;
      const found = data.discoveries.find(d => d.company_id === pending.values.company_id);
      if ($("#dialog").open && $("#addLink")) openAddCamp({...pending.values, found, error: !found ? data.job.message : ""});
    }
    if (pendingReview && data.job.id === pendingReview.id && !data.busy) {
      if (["completed", "issues"].includes(data.job.state)) {
        pendingReview.keys.forEach(key => { reviewDrafts.delete(key); reviewSelected.delete(key); });
        proposalSignature = "";
      }
      pendingReview = null;
    }
    if (!$("#checkin").value) $("#checkin").value = data.today;
    selected = new Set(
      [...selected].filter((k) => data.entries.some((e) => e.key === k)),
    );
    $("#connection").textContent = data.preview ? "미리보기 · 연결 전" : data.cloud ? "클라우드 연결됨" : "PC 연결됨";
    $("#version").textContent = "v" + data.version + (data.cloud ? " · Google Drive" : " · 로컬 데이터");
    if (data.cloud) {
      for (const link of document.querySelectorAll(".file-links a")) {
        const role = link.dataset.role || link.getAttribute("href").split("/").at(-1);
        link.dataset.role = role;
        link.href = data.downloads?.[role] || data.drive_url;
        link.removeAttribute("download");
        link.target = "_blank";
        link.rel = "noopener";
      }
    }
    render();
    if (previousEntry !== null && !drawerDirty && previousEntry !== JSON.stringify(current())) renderInspector();
  } catch (e) {
    $("#connection").textContent = "연결 끊김";
    if (!data)
      $("#catalogRows").innerHTML =
        `<tr><td colspan="5">${empty(e.message, "wifi-off")}</td></tr>`;
  } finally {
    if (requesting === request) requesting = null;
  }
}
function render() {
  if (!data) return;
  $("#jobAlert").classList.toggle("hidden", !["failed", "interrupted"].includes(data.job.state));
  $("#jobAlert").textContent =
    ["failed", "interrupted"].includes(data.job.state) ? data.job.message : "";
  const active = data.entries.filter((e) => !e.archived),
    normalCount = active.filter(normal).length;
  $("#metricTotal").textContent = active.length;
  $("#metricRooms").textContent =
    active.reduce((s, e) => s + e.rooms.length, 0).toLocaleString() + " 객실";
  $("#metricNormal").textContent = normalCount;
  $("#metricIssue").textContent = active.filter(needsAttention).length;
  $("#metricChanged").textContent =
    active.filter(pendingCollection).length + " 항목 재수집 대기";
  $("#addCamp").disabled = data.busy;
  $("#collectIssues").disabled = data.busy || !active.some(needsAttention);
  const latest = data.entries
    .map((e) => e.result?.checked_at || "")
    .sort()
    .at(-1);
  $("#metricLast").textContent = latest
    ? latest.slice(5, 10).replace("-", ".")
    : "-";
  $("#metricDate").textContent = latest
    ? latest.slice(11, 16) + " 확인"
    : "수집 기록 없음";
  $("#changeCount").textContent = reviewEntries().length;
  $("#collectAll").disabled = data.busy;
  $("#stop").classList.toggle("hidden", !data.busy);
  $("#jobLabel").textContent = data.busy
    ? kinds[data.job.kind] || "작업 중"
    : "활동 로그";
  $("#jobMessage").textContent = data.job.message;
  $("#progress").max = Math.max(data.job.total, 1);
  $("#progress").value = data.job.completed;
  $("#selectedCount").textContent = selected.size + "개 선택";
  const inTrash = $("#statusFilter").value === "archived";
  for (const id of ["scanSelected", "collectSelected", "bulkEdit"])
    $("#" + id).disabled = data.busy || !selected.size || inTrash;
  const remove = $("#archive"), actionLabel = inTrash ? "선택 항목 복원" : "선택 항목 삭제";
  remove.disabled = data.busy || !selected.size;
  if (remove.title !== actionLabel) remove.innerHTML = icon(inTrash ? "archive-restore" : "trash-2");
  remove.title = actionLabel;
  remove.setAttribute("aria-label", actionLabel);
  remove.classList.toggle("danger", !inTrash);
  if (view === "catalog") renderCatalog();
  if (view === "summary") renderSummary();
  if (view === "changes") renderChanges();
  if (view === "discovery") renderDiscoveries();
  if (view === "history") renderJobs();
  icons();
}
function businessValue(e) {
  const r = e.result;
  const valid = normal(e) && !e.archived && Number.isInteger(r.total) && r.total > 0
    && r.total === e.rooms.length && Number.isInteger(r.reserved) && r.reserved >= 0 && r.reserved <= r.total;
  const priced = valid && Number.isFinite(e.price_amount) && e.price_amount >= 0;
  return {valid, rate: valid ? r.reserved / r.total : null, revenue: priced ? r.reserved * e.price_amount : null};
}
function renderSummary() {
  const query = $("#summarySearch").value.trim().toLocaleLowerCase();
  const entries = data.entries.filter(e => !e.archived && [e.major, e.sheet_title, e.company_id].join(" ").toLocaleLowerCase().includes(query));
  const signature = JSON.stringify([query, data.busy, $("#checkin").value, entries.map(e => [e.key, e.major, e.sheet_title, e.price, e.price_amount, e.rooms.length, e.result, e.changed, e.proposal?.changed, e.proposal?.error])]);
  if (signature === businessSignature) return;
  businessSignature = signature;
  const valid = entries.filter(e => businessValue(e).valid), priced = valid.filter(e => businessValue(e).revenue !== null);
  const total = valid.reduce((n, e) => n + e.result.total, 0), reserved = valid.reduce((n, e) => n + e.result.reserved, 0);
  const revenue = priced.reduce((n, e) => n + businessValue(e).revenue, 0);
  $("#businessMetrics").innerHTML = `<div><span>가동률 · 마감 기준</span><strong>${total ? (100 * reserved / total).toFixed(1) + "%" : "-"}</strong><small>확인된 ${reserved} / ${total}객실</small></div><div><span>추정 매출</span><strong>${priced.length ? money(revenue) : "-"}</strong><small>금액 확인 ${priced.length}/${entries.length}항목</small></div><div><span>집계 가능한 항목</span><strong>${valid.length} / ${entries.length}</strong><small>미확인·이전 날짜 제외 ${entries.length - valid.length}개</small></div>`;
  $("#summaryCoverage").textContent = `${$("#checkin").value} · ${entries.length}개 항목`;
  $("#businessRows").innerHTML = entries.map(e => {
    const value = businessValue(e);
    return `<tr><td><strong>${esc(e.major)}</strong><small>${esc(e.sheet_title)}</small></td><td>${value.valid ? `${e.result.reserved} / ${e.rooms.length}` : `- / ${e.rooms.length}`}</td><td>${value.rate === null ? "-" : (value.rate * 100).toFixed(1) + "%"}</td><td>${money(e.price_amount)}</td><td>${value.revenue === null ? (value.valid ? "금액 미입력" : "집계 제외") : money(value.revenue)}</td><td>${badge(e)}</td><td><button class="icon-button" data-summary-edit="${e.key}" title="객실·금액 수정" aria-label="${esc(e.sheet_title)} 수정">${icon("pencil")}</button><button class="icon-button" data-summary-history="${e.key}" title="누적 현황" aria-label="${esc(e.sheet_title)} 누적 현황">${icon("chart-no-axes-combined")}</button><button class="icon-button" data-summary-collect="${e.key}" title="이 항목 재수집" aria-label="${esc(e.sheet_title)} 재수집" ${data.busy ? "disabled" : ""}>${icon("refresh-cw")}</button></td></tr>`;
  }).join("") || `<tr><td colspan="7">${empty("야영장을 추가해 주세요", "tent-tree")}</td></tr>`;
  $$("[data-summary-edit]").forEach(button => {
    const key = button.dataset.summaryEdit, entry = entries.find(e => e.key === key);
    const name = button.closest("tr").querySelector("td");
    name.innerHTML = `<button class="camp-name" data-summary-history="${key}" aria-label="${esc(entry.sheet_title)} 추이 보기"><strong>${esc(entry.major)}</strong><small>${esc(entry.sheet_title)}</small></button>`;
    button.parentElement.insertAdjacentHTML("beforeend", `<button class="icon-button" data-summary-copy="${key}" title="복사하여 추가" aria-label="${esc(entry.sheet_title)} 복사" ${data.busy ? "disabled" : ""}>${icon("copy-plus")}</button><button class="icon-button danger" data-summary-delete="${key}" title="목록에서 삭제" aria-label="${esc(entry.sheet_title)} 삭제" ${data.busy ? "disabled" : ""}>${icon("trash-2")}</button>`);
  });
}
function cleanHistory(history) {
  const days = new Map();
  for (const h of history) {
    const date = String(h.date || "").slice(0, 10), stamp = Date.parse(date + "T00:00:00Z");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(stamp) || new Date(stamp).toISOString().slice(0, 10) !== date) continue;
    if (!Number.isInteger(h.total) || h.total <= 0 || !Number.isInteger(h.reserved) || h.reserved < 0 || h.reserved > h.total) continue;
    days.set(date, {...h, date, stamp, rate: h.reserved / h.total, revenue: Number.isFinite(h.revenue) && h.revenue >= 0 ? h.revenue : null});
  }
  return [...days.values()].sort((a, b) => a.stamp - b.stamp);
}
async function openTrend(key) {
  const entry = data.entries.find(e => e.key === key);
  if (!entry) return;
  const request = {key, entry, days:90, history:[]};
  activeTrend = request;
  openDialog(`${entry.major} / ${entry.sheet_title} · 누적 추이`, '<div id="trendLoading" role="status">누적 자료 불러오는 중</div>', [["닫기", () => $("#dialog").close()]]);
  try {
    const response = await api("/api/history?key=" + encodeURIComponent(key));
    if (activeTrend !== request || !$("#dialog").open || !$("#trendLoading")) return;
    request.history = cleanHistory(response.history || []);
    if (!request.history.length) {
      $("#dialogBody").innerHTML = empty("저장된 정상 수집 자료가 없습니다", "chart-no-axes-combined"); icons(); return;
    }
    $("#dialogBody").innerHTML = `<div class="trend-toolbar"><div class="review-filters" role="group" aria-label="추이 기간">${[7,30,90,0].map(days=>`<button data-trend-days="${days}" aria-pressed="${days === 90}">${days ? days + "일" : "전체"}</button>`).join("")}</div><span id="trendPeriod" class="muted"></span></div><div id="trendMetrics" class="business-metrics"></div><div class="trend-charts"><section><h3>가동률 추이</h3><canvas id="trendRate" aria-label="날짜별 가동률 그래프"></canvas><output id="trendRateValue"></output></section><section><h3>예상 매출 추이</h3><canvas id="trendRevenue" aria-label="날짜별 예상 매출 그래프"></canvas><output id="trendRevenueValue"></output></section></div><div class="trend-table-scroll"><table class="history-table"><thead><tr><th>체크인 날짜</th><th>마감 / 전체</th><th>가동률</th><th>기준 금액</th><th>예상 매출</th></tr></thead><tbody id="trendRows"></tbody></table></div><p class="history-disclaimer">체크인 날짜별 마지막 정상 수집 기록입니다. 같은 날짜의 재수집은 중복 합산하지 않습니다. 마감 기준 추정치이며 실제 매출과 다를 수 있습니다. 수집하지 않은 날짜와 미입력 금액은 0으로 채우지 않습니다.</p>`;
    $$("[data-trend-days]").forEach(button => button.onclick = () => { request.days = Number(button.dataset.trendDays); renderTrend(); });
    renderTrend(); icons();
  } catch (error) {
    if (activeTrend === request && $("#trendLoading")) $("#trendLoading").textContent = error.message;
  }
}
function renderTrend() {
  if (!activeTrend || !$("#trendRows")) return;
  const {history, days} = activeTrend, end = history.at(-1).stamp;
  const points = history.filter(h => !days || h.stamp >= end - (days - 1) * 86400000);
  const total = points.reduce((n,h)=>n+h.total,0), reserved = points.reduce((n,h)=>n+h.reserved,0);
  const priced = points.filter(h=>h.revenue !== null), revenue = priced.reduce((n,h)=>n+h.revenue,0);
  $("#trendPeriod").textContent = `${points[0].date} ~ ${points.at(-1).date}`;
  $$("[data-trend-days]").forEach(button => button.setAttribute("aria-pressed", String(Number(button.dataset.trendDays) === days)));
  $("#trendMetrics").innerHTML = `<div><span>기간 가동률</span><strong>${(100 * reserved / total).toFixed(1)}%</strong><small>확인된 객실수 가중 평균</small></div><div><span>기간 예상 매출 합계</span><strong>${priced.length ? money(revenue) : "-"}</strong><small>금액 확인 ${priced.length}/${points.length}일</small></div><div><span>누적 수집일</span><strong>${points.length}일</strong><small>체크인 날짜 기준</small></div>`;
  $("#trendRows").innerHTML = points.slice().reverse().map(h=>`<tr><td>${esc(h.date)}</td><td>${h.reserved} / ${h.total}</td><td>${(h.rate*100).toFixed(1)}%</td><td>${esc(h.price || "-")}</td><td>${money(h.revenue)}</td></tr>`).join("");
  drawTrendChart($("#trendRate"), $("#trendRateValue"), points, "rate", "#13734d");
  drawTrendChart($("#trendRevenue"), $("#trendRevenueValue"), points, "revenue", "#2563a6");
}
function drawTrendChart(canvas, output, points, field, color) {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = 190;
  canvas.width = Math.max(1, w * dpr); canvas.height = h * dpr;
  const c = canvas.getContext("2d"); c.scale(dpr,dpr);
  const valid = points.filter(p => p[field] !== null);
  const max = field === "rate" ? 1 : Math.max(1,...valid.map(p=>p[field]));
  const left = 52, right = Math.max(left + 1,w - 12), top = 18, bottom = 156;
  const first = points[0].stamp, last = points.at(-1).stamp;
  const x = p => first === last ? (left+right)/2 : left+(right-left)*(p.stamp-first)/(last-first);
  const y = p => bottom-(bottom-top)*p[field]/max;
  c.font = "11px Malgun Gothic"; c.lineWidth = 1;
  for (let step=0;step<=4;step++) {
    const value=max*step/4, py=bottom-(bottom-top)*step/4;
    c.strokeStyle="#e2e7e7"; c.beginPath(); c.moveTo(left,py); c.lineTo(right,py); c.stroke();
    c.fillStyle="#6d777a"; c.textAlign="right";
    c.fillText(field === "rate" ? Math.round(value*100)+"%" : value >= 10000 ? (value/10000).toFixed(1)+"만" : Math.round(value).toLocaleString(),left-7,py+4);
  }
  c.strokeStyle=color; c.fillStyle=color; c.lineWidth=2;
  let previous=null;
  for (const point of points) {
    if (point[field] === null) {previous=null; continue;}
    if (previous && point.stamp-previous.stamp === 86400000) {c.beginPath();c.moveTo(x(previous),y(previous));c.lineTo(x(point),y(point));c.stroke();}
    c.beginPath();c.arc(x(point),y(point),3,0,Math.PI*2);c.fill();previous=point;
  }
  c.fillStyle="#6d777a"; c.textAlign="left"; c.fillText(points[0].date.slice(5),left,181);
  c.textAlign="right"; c.fillText(points.at(-1).date.slice(5),right,181);
  if (!valid.length) {c.textAlign="center"; c.fillText("금액 입력 기록 없음",(left+right)/2,80);}
  const label = p => `${p.date} · ${field === "rate" ? (p.rate*100).toFixed(1)+"%" : money(p.revenue)}`;
  output.textContent = label(points.at(-1));
  canvas.onpointermove = event => {
    const px=event.clientX-canvas.getBoundingClientRect().left;
    const nearest=points.reduce((best,p)=>Math.abs(x(p)-px)<Math.abs(x(best)-px)?p:best,points[0]);
    output.textContent=label(nearest);
  };
  canvas.onpointerleave = () => output.textContent=label(points.at(-1));
}
function renderCatalog() {
  const entries = visibleEntries();
  if (!drawerDirty && !entries.some(e => e.key === activeKey)) {
    activeKey = entries[0]?.key || "";
    renderInspector();
  }
  const signature = JSON.stringify([
    entries.map((e) => [
      e.key,
      e.major,
      e.sheet_title,
      e.price,
      e.rooms.length,
      e.region,
      e.changed,
      e.result,
      e.archived,
    ]),
    [...selected],
    activeKey,
    $("#checkin").value,
  ]);
  if (signature !== tableSignature) {
    tableSignature = signature;
    $("#catalogRows").innerHTML =
      entries
        .map(
          (e) =>
            `<tr data-key="${e.key}" class="${e.key === activeKey ? "selected-row" : ""}" tabindex="0" aria-label="${esc(e.major + " " + e.sheet_title)}"><td><input type="checkbox" data-select="${e.key}" ${selected.has(e.key) ? "checked" : ""} aria-label="${esc(e.sheet_title)} 선택"></td><td><span class="row-title">${esc(e.sheet_title || e.major)}</span><div class="row-sub"><span class="category">${esc(e.major)}</span><span>${esc(e.company_id)}</span>${e.region ? `<span>${esc(e.region)}</span>` : ""}</div></td><td><div class="counts">${e.result?.reserved ?? "-"} <span>/ ${e.result?.total ?? e.rooms.length}</span></div></td><td>${badge(e)}</td><td class="last-check">${formatTime(e.result?.checked_at)}</td></tr>`,
        )
        .join("") ||
      `<tr><td colspan="5">${empty("해당 항목이 없습니다", "search")}</td></tr>`;
    $("#tableCount").textContent = "전체 " + entries.length + "개 항목";
    $("#selectAll").checked =
      entries.length > 0 && entries.every((e) => selected.has(e.key));
    $("#selectAll").indeterminate =
      entries.some((e) => selected.has(e.key)) && !$("#selectAll").checked;
  }
  if (!activeKey && entries.length) {
    activeKey = entries[0].key;
    renderInspector();
  } else if (!$("#inspector").innerHTML) renderInspector();
}
function inspectorForm(e) {
  return `<div class="form-grid"><label>대분류<input id="editMajor" value="${esc(e.major)}"></label><label>중분류 / 시트명<input id="editTitle" value="${esc(e.sheet_title)}" maxlength="31"></label><label>금액<input id="editPrice" value="${esc(e.price)}" placeholder="예: 50,000원"></label><label>지역<input id="editRegion" value="${esc(e.region)}" placeholder="미입력"></label></div>${e.price && e.price_amount === null ? '<div class="price-note">금액 단위 확인 필요 · 추정 매출 계산 제외</div>' : ""}`;
}
function renderInspector() {
  const e = current();
  if (!e) {
    $("#inspector").innerHTML = empty("항목을 선택해 주세요", "panel-right");
    icons();
    return;
  }
  drawerDirty = false;
  drawerRevision = data.revision;
  // Escape protocol slashes to preserve URLs through HtmlService sanitization.
  const roomUrl = "https:\x2f\x2fm.place.naver.com/accommodation/" + encodeURIComponent(e.company_id) + "/room";
  $("#inspector").innerHTML =
    `<div class="inspector-head"><h2>${esc(e.sheet_title || e.major)}</h2>${badge(e)}</div><div class="business-id">업체번호 ${esc(e.company_id)}<a href="${esc(roomUrl)}" target="_blank" rel="noopener" title="네이버 객실 페이지">${icon("external-link")}</a></div>${inspectorForm(e)}<div class="item-toolbar"><button id="classifyItems">${icon("list-filter")}상품 판별</button><small>객실 · 제외 · 확인 필요</small></div><div class="tabs"><button data-tab="rooms" class="${inspectorTab === "rooms" ? "active" : ""}">객실 <span id="roomCount">${e.rooms.length}</span></button><button data-tab="diff" class="${inspectorTab === "diff" ? "active" : ""}">변경 비교</button><button data-tab="history" class="${inspectorTab === "history" ? "active" : ""}">현황</button></div><div id="inspectorContent"></div><div class="inspector-actions"><button id="saveAndCollect" class="primary">${icon("save")}저장 후 선택 수집</button><div><button id="saveEntry">${icon("save")}DB 저장</button><button id="copyEntry">${icon("copy-plus")}복사하여 추가</button></div></div>`;
  $("#inspectorContent").insertAdjacentHTML("beforebegin", `<label class="manual-authority"><input id="editManual" type="checkbox" checked>내 입력 우선 확정 <span class="muted">${e.room_authority === "manual" ? "수동 목록 적용 중" : "자동 판별 적용 중"}</span></label>`);
  renderInspectorTab(e);
  $$("#inspector input,#inspector textarea").forEach((el) =>
    el.addEventListener("input", () => (drawerDirty = true)),
  );
  $("#saveEntry").onclick = guarded(() => saveInspector(false));
  $("#classifyItems").onclick = guarded(() => openItemRules(e.key));
  $("#classifyItems").innerHTML = `${icon("list-checks")}변경 확인`;
  $("#saveAndCollect").onclick = guarded(() => saveInspector(true));
  $("#copyEntry").onclick = () => editModal([e], true);
  if (e.archived) {
    $$("#inspector input,#inspector textarea,#saveEntry,#saveAndCollect,#copyEntry,#classifyItems").forEach(el => el.disabled = true);
  }
  $$("[data-tab]").forEach(
    (b) =>
      (b.onclick = () => {
        if (drawerDirty) {
          toast("입력한 내용을 먼저 저장해 주세요.");
          return;
        }
        inspectorTab = b.dataset.tab;
        renderInspector();
      }),
  );
  icons();
}
function renderInspectorTab(e) {
  if (inspectorTab === "rooms") {
    $("#inspectorContent").innerHTML =
      `<div class="room-heading"><strong>객실명 목록</strong><span class="muted">${e.rooms.length}개</span></div><textarea id="editRooms" class="room-editor" aria-label="객실명 목록">${esc(e.rooms.join("\n"))}</textarea>${e.result && e.result.status !== "정상" ? `<div class="warning-note">${esc(e.result.status)}</div>` : ""}`;
    $("#editRooms").oninput = () => {
      $("#roomCount").textContent = $("#editRooms")
        .value.split("\n")
        .filter((v) => v.trim()).length;
      drawerDirty = true;
    };
  } else if (inspectorTab === "diff") {
    const p = e.proposal;
    $("#inspectorContent").innerHTML = p
      ? `<div class="room-diff"><p>${p.error ? esc(p.error) : p.changed ? "객실 구성이 달라졌습니다." : "객실 구성 변경 없음"}</p>${diffNotes(p)}</div><button id="openChanges">${icon("git-compare-arrows")}변경 점검 열기</button>`
      : empty("점검 기록 없음", "scan-line");
    if ($("#openChanges"))
      $("#openChanges").onclick = () => showView("changes");
  } else {
    $("#inspectorContent").innerHTML = empty(
      "현황 불러오는 중",
      "chart-no-axes-combined",
    );
    loadHistory(e.key);
  }
}
async function loadHistory(key) {
  try {
    const { history } = await api(
      "/api/history?key=" + encodeURIComponent(key),
    );
    if (key !== activeKey || inspectorTab !== "history") return;
    $("#inspectorContent").innerHTML = history.length
      ? `<canvas id="historyChart" class="chart" aria-label="마감 비율 추이"></canvas><div class="room-diff"><table class="history-table"><thead><tr><th>체크인</th><th>마감/전체</th><th>마감률</th><th>추정 매출</th></tr></thead><tbody>${history
          .slice()
          .reverse()
          .map(
            (h) =>
              `<tr><td>${esc(h.date.slice(5))}</td><td>${h.reserved}/${h.total}</td><td>${h.rate === null ? "-" : Math.round(h.rate * 100) + "%"}</td><td>${money(h.revenue)}</td></tr>`,
          )
          .join(
            "",
          )}</tbody></table></div><p class="history-disclaimer">마감 표시는 실제 예약과 다를 수 있습니다. 추정 매출은 입력 금액 기준이며 실제 매출이 아닙니다.</p>`
      : empty("저장된 현황 없음", "chart-no-axes-combined");
    if (history.length) drawChart(history);
    if (history.length) {
      $("#inspectorContent").insertAdjacentHTML("afterbegin", `<button id="openFullTrend">${icon("chart-no-axes-combined")}가동률·예상 매출 추이</button>`);
      $("#openFullTrend").onclick = guarded(() => openTrend(key));
    }
    icons();
  } catch (e) {
    toast(e.message, true);
  }
}
function drawChart(history) {
  const canvas = $("#historyChart"),
    dpr = window.devicePixelRatio || 1,
    w = canvas.clientWidth,
    h = 120;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const c = canvas.getContext("2d");
  c.scale(dpr, dpr);
  c.strokeStyle = "#e2e7e7";
  c.lineWidth = 1;
  for (let y of [16, 57, 98]) {
    c.beginPath();
    c.moveTo(0, y);
    c.lineTo(w, y);
    c.stroke();
  }
  const points = history.slice(-24).filter((v) => v.rate !== null);
  c.strokeStyle = "#13734d";
  c.lineWidth = 2;
  c.beginPath();
  points.forEach((p, i) => {
    const x = 8 + ((w - 16) * i) / Math.max(points.length - 1, 1),
      y = 98 - Math.min(1, p.rate) * 82;
    i ? c.lineTo(x, y) : c.moveTo(x, y);
  });
  c.stroke();
  c.fillStyle = "#13734d";
  points.forEach((p, i) => {
    c.beginPath();
    c.arc(
      8 + ((w - 16) * i) / Math.max(points.length - 1, 1),
      98 - Math.min(1, p.rate) * 82,
      2.5,
      0,
      Math.PI * 2,
    );
    c.fill();
  });
  c.fillStyle = "#6d777a";
  c.font = "10px Malgun Gothic";
  c.fillText(points[0]?.date.slice(5) || "", 0, 117);
  c.textAlign = "right";
  c.fillText(points.at(-1)?.date.slice(5) || "", w, 117);
}
async function saveInspector(collect) {
  const e = current();
  if (!e) return;
  const edit = {
    ...e,
    major: $("#editMajor").value,
    sheet_title: $("#editTitle").value,
    price: $("#editPrice").value,
    region: $("#editRegion").value,
    rooms: $("#editRooms")?.value ?? e.rooms,
    room_authority: $("#editManual").checked ? "manual" : "auto",
  };
  const saved = await api("/api/apply", {
    revision: drawerRevision,
    edits: [edit],
    force_manual: edit.room_authority === "manual",
    ...(collect && data.cloud ? {collect_after: true, date: $("#checkin").value} : {}),
  });
  if (saved.queued) {
    pendingSave = {id: saved.job_id, key: e.key};
    drawerDirty = true;
    await refresh(true);
    toast("DB 저장 후 선택 항목 재수집을 요청했습니다.");
    return;
  }
  drawerDirty = false;
  activeKey = saved.keys[0];
  await refresh(true);
  renderInspector();
  toast("객실 DB 반영 및 원본 백업 완료");
  if (collect) await start("collect", saved.keys);
}
async function start(kind, keys, all = false, extra = {}) {
  if (drawerDirty) throw new Error("입력한 내용을 먼저 저장해 주세요.");
  await api("/api/start", {
    kind,
    date: $("#checkin").value,
    revision: data.revision,
    keys,
    all,
    ...extra,
  });
  await refresh(true);
  toast((kinds[kind] || "작업") + " 시작");
}
function openDialog(title, body, actions = []) {
  $("#dialogTitle").textContent = title;
  $("#dialogBody").onchange = null;
  $("#dialogBody").innerHTML = body;
  $("#dialogActions").innerHTML = "";
  for (const [text, fn, primary] of actions) {
    const b = document.createElement("button");
    b.textContent = text;
    if (primary) b.className = "primary";
    b.onclick = guarded(async () => {
      const controls = $$("#dialog input,#dialog textarea,#dialog select,#dialog button").map(el => [el, el.disabled]);
      controls.forEach(([el]) => el.disabled = true);
      const cancel = $("#dialog").oncancel;
      $("#dialog").oncancel = event => event.preventDefault();
      try {
        await fn();
      } finally {
        controls.forEach(([el, disabled]) => el.disabled = disabled);
        $("#dialog").oncancel = cancel;
      }
    });
    $("#dialogActions").append(b);
  }
  $("#dialog").showModal();
  icons();
}
function confirmListRemoval(keys, archived = true) {
  if (data.busy) throw new Error("작업이 끝난 뒤 삭제하거나 복원해 주세요.");
  const entries = data.entries.filter(e => keys.includes(e.key) && !!e.archived !== archived);
  if (!entries.length) throw new Error("삭제하거나 복원할 항목을 선택해 주세요.");
  const targetKeys = entries.map(e => e.key), editRevision = data.revision;
  const hasDraft = entries.some(e => reviewDrafts.has(e.key)) || (drawerDirty && targetKeys.includes(activeKey));
  openDialog(
    archived ? `선택한 ${entries.length}개 항목 삭제` : `선택한 ${entries.length}개 항목 복원`,
    `<p>${archived ? "이 항목들을 목록과 수집 대상에서 삭제합니다. 기존 DB와 과거 집계자료는 보존되며, 통합 관리의 휴지통에서 복원할 수 있습니다." : "이 항목들을 목록과 수집 대상으로 복원합니다."}</p><ul class="removal-list">${entries.map(e => `<li><strong>${esc(e.major)} / ${esc(e.sheet_title)}</strong><small>${esc(e.company_id)} · 객실 ${e.rooms.length}개</small></li>`).join("")}</ul>${hasDraft ? '<p class="warning-note">선택한 항목의 저장하지 않은 수정 내용은 취소됩니다. 다른 항목의 수정 내용은 유지됩니다.</p>' : ""}`,
    [
      ["취소", () => $("#dialog").close()],
      [archived ? "삭제" : "복원", async () => {
        await api("/api/archive", {keys: targetKeys, archived, revision: editRevision});
        targetKeys.forEach(key => { selected.delete(key); reviewSelected.delete(key); reviewDrafts.delete(key); });
        if (targetKeys.includes(activeKey)) {
          activeKey = "";
          drawerDirty = false;
          renderInspector();
        }
        proposalSignature = tableSignature = "";
        $("#dialog").close();
        await refresh(true);
        toast(`${entries.length}개 항목 ${archived ? "삭제 완료 · 휴지통에서 복원 가능" : "복원 완료"}`);
      }, true],
    ],
  );
}
function editModal(entries, copy = false) {
  if (data.busy) throw new Error("진행 중인 작업이 끝나면 수정할 수 있습니다.");
  const editRevision = data.revision;
  const edits = entries.map((e) => ({
    ...e,
    key: copy ? null : e.key,
    sheet_title: copy ? e.sheet_title.slice(0, 27) + " 복사" : e.sheet_title,
  }));
  async function save(collect) {
    const changed = edits.map((e, i) => {
      const row = $(`[data-edit="${i}"]`);
      const values = Object.fromEntries(["major", "sheet_title", "price", "rooms"].map(field => [field, row.querySelector(`[data-field="${field}"]`).value]));
      return {...e, ...values, company_id: copy ? row.querySelector('[data-field="company_id"]').value : e.company_id, room_authority: "manual"};
    });
    const saved = await api("/api/apply", {revision: editRevision, edits: changed, force_manual: true,
      ...(collect && data.cloud ? {collect_after: true, wait_for_completion: true, date: $("#checkin").value} : {})});
    $("#dialog").close();
    entries.forEach(e => { reviewDrafts.delete(e.key); reviewSelected.delete(e.key); });
    selected = new Set(saved.keys); activeKey = saved.keys[0];
    await refresh(true); renderInspector();
    toast(`${saved.keys.length}개 항목 저장 완료${saved.collection_state === "issues" ? " · 수집 확인 필요" : saved.collection_state ? " · 수집 완료" : ""}`);
    if (collect && !data.cloud) await start("collect", saved.keys);
  }
  openDialog(
    copy ? "복사하여 추가" : entries.length === 1 ? "객실·금액 수정" : "선택 항목 일괄 편집",
    edits
      .map(
        (e, i) =>
          `<div class="bulk-row" data-edit="${i}"><div class="row-sub">업체번호 ${copy ? `<input data-field="company_id" value="${esc(e.company_id)}" aria-label="복사할 업체번호">` : esc(e.company_id)}</div><div class="form-grid"><label>대분류<input data-field="major" value="${esc(e.major)}"></label><label>중분류 / 시트명<input data-field="sheet_title" maxlength="31" value="${esc(e.sheet_title)}"></label><label>금액<input data-field="price" value="${esc(e.price)}"></label></div><textarea data-field="rooms" aria-label="${esc(e.sheet_title)} 객실명">${esc(e.rooms.join("\n"))}</textarea></div>`,
      )
      .join(""),
    [["취소", () => $("#dialog").close()], ["저장", () => save(false)], ["저장 후 이 항목 수집", () => save(true), true]],
  );
}
function parsePlaceId(value) {
  value = value.trim();
  if (/^\d{5,15}$/.test(value)) return value;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || !["m.place.naver.com", "pcmap.place.naver.com", "map.naver.com"].includes(url.hostname)) return "";
    return url.pathname.match(/\/(?:accommodation|place|camping)\/(\d{5,15})(?:\/|$)/)?.[1] || "";
  } catch { return ""; }
}
function openAddCamp(values = {}) {
  if (drawerDirty) throw new Error("입력한 수정 내용을 먼저 저장해 주세요.");
  const found = values.found;
  const name = values.major || found?.name || "";
  const rooms = values.rooms || (found?.rooms || []).join("\n");
  const editRevision = data.revision;
  function readForm() {
    const company_id = parsePlaceId($("#addLink").value);
    if (!company_id) throw new Error("네이버 플레이스 주소 또는 업체번호를 입력해 주세요. 단축 주소 대신 업체 페이지 주소를 사용해 주세요.");
    return {company_id, major: $("#addName").value.trim(), sheet_title: $("#addTitle").value.trim(), price: $("#addPrice").value.trim(), rooms: $("#addRooms").value};
  }
  async function save(collect) {
    if (data.busy) throw new Error("객실 불러오기가 끝난 뒤 저장해 주세요.");
    const edit = readForm();
    if (!edit.major) throw new Error("야영장 이름을 입력해 주세요.");
    edit.sheet_title ||= edit.major;
    if (!edit.rooms.trim()) throw new Error("객실 불러오기를 누르거나 객실명을 입력해 주세요.");
    const saved = await api("/api/apply", {revision: editRevision, edits: [{...edit, room_authority: "manual"}],
      ...(collect && data.cloud ? {collect_after: true, wait_for_completion: true, date: $("#checkin").value} : {})});
    $("#dialog").close(); activeKey = saved.keys[0]; selected = new Set(saved.keys);
    await refresh(true); showView("catalog"); renderInspector();
    toast("야영장을 추가했습니다." + (saved.collection_state === "issues" ? " 수집 확인이 필요합니다." : ""));
    if (collect && !data.cloud) await start("collect", saved.keys);
  }
  openDialog("야영장 추가", `<div class="add-camp-form"><label>네이버 플레이스 주소 / 업체번호<input id="addLink" value="${esc(values.company_id || "")}" placeholder="네이버 플레이스 주소 또는 숫자 업체번호"></label><button id="loadAddRooms">${icon("download")}객실 불러오기</button><div class="form-grid"><label>야영장 이름<input id="addName" value="${esc(name)}"></label><label>분류명 / 시트명<input id="addTitle" maxlength="31" value="${esc(values.sheet_title || "")}" placeholder="비워두면 야영장 이름"></label><label>객실당 기준 금액<input id="addPrice" value="${esc(values.price || "")}" placeholder="예: 50,000원"></label></div><label>객실명<textarea id="addRooms" rows="8" placeholder="A-1&#10;A-2">${esc(rooms)}</textarea></label><p id="addFeedback" role="status">${esc(values.error || (found ? (found.rooms.length ? `객실 후보 ${found.rooms.length}개 · 공지·쿠폰이 포함돼 있으면 지운 뒤 저장하세요.` : "불러온 객실이 없습니다. 직접 입력할 수 있습니다.") : ""))}</p></div>`, [["취소", () => { pendingAdd = null; $("#dialog").close(); }], ["저장", () => save(false)], ["저장 후 수집", () => save(true), true]]);
  $("#loadAddRooms").onclick = guarded(async () => {
    if (data.busy) throw new Error("진행 중인 작업이 끝난 뒤 불러와 주세요.");
    const values = readForm();
    const result = await api("/api/start", {kind: "verify", links: values.company_id, revision: data.revision, date: $("#checkin").value});
    pendingAdd = {id: result.job_id, values};
    $("#addFeedback").textContent = "객실 불러오는 중 · 완료되면 이 창에 표시됩니다.";
    $$("#dialog input,#dialog textarea,#loadAddRooms,#dialogActions button").forEach(el => el.disabled = true);
    await refresh(true);
  });
}
function diffNotes(p) {
  return `<div class="diff-notes">${(p.added || []).map((r) => `<span class="added">+ ${esc(r)}</span>`).join("")}${(p.removed || []).map((r) => `<span class="removed">− ${esc(r)}</span>`).join("")}</div>`;
}
function reviewItems(entry) {
  const groups = entry.item_groups || {};
  return [...(groups.review || []), ...(groups.excluded || []), ...(groups.room || [])];
}
function reviewComparison(entry) {
  const items = reviewItems(entry), p = entry.proposal || {};
  const members = data.entries.filter(e => e.company_id === entry.company_id && !e.archived);
  const normalize = value => value.replace(/[\s\u200b]/g, "");
  const labels = {room: "객실 후보", excluded: "제외", review: "확인 필요"};
  const list = items.map(item => {
    const owners = members.filter(e => e.rooms.some(room => normalize(room) === normalize(item.title)));
    return `<li><strong>${esc(item.title)}</strong><span>${esc(owners.map(e => e.sheet_title).join(" · "))}${owners.length ? " · " : ""}<b class="${item.decision === "excluded" ? "amber" : ""}">${esc(labels[item.decision] || "확인 필요")}</b></span></li>`;
  }).join("");
  return `<div class="change-grid review-comparison"><section><h3>현재 DB · ${entry.rooms.length}개</h3><ul class="review-room-list" aria-label="${esc(entry.sheet_title)} 현재 DB">${entry.rooms.map(room => `<li>${esc(room)}</li>`).join("")}</ul></section><section><h3>네이버에서 수집된 상품 · ${items.length}개${members.length > 1 ? " · 업체 전체" : ""}</h3>${items.length ? `<ul class="review-room-list" aria-label="${esc(entry.sheet_title)} 수집된 상품">${list}</ul>` : `<div class="review-no-data" role="status">객실 정보를 가져오지 못했습니다.<strong>기존 DB ${entry.rooms.length}개 유지</strong></div>`}</section></div>${items.length && proposalReady(entry) ? diffNotes(p) : ""}`;
}
function openItemRules(key) {
  if (drawerDirty) throw new Error("편집한 DB 내용을 먼저 저장해 주세요.");
  const entry = data.entries.find((e) => e.key === key);
  const groups = entry.item_groups || {};
  const items = reviewItems(entry);
  if (!items.length) {
    openDialog("상품 판별", empty("상품 판별 기록 없음", "list-filter"), [
      [
        "선택 업체 점검",
        async () => {
          $("#dialog").close();
          await start("scan", [key]);
        },
        true,
      ],
    ]);
    return;
  }
  const dbRevision = data.revision,
    rulesRevision = entry.rules_revision;
  const labels = { room: "객실", excluded: "제외", review: "확인 필요" };
  const modes = {
    auto: "자동 판별",
    include: "객실로 포함",
    exclude: "항상 제외",
  };
  const drafts = items.map((item) => item.mode);
  const checked = new Set();
  let filter = "all",
    query = "";
  const visibleIndices = () =>
    items.flatMap((item, i) =>
      (filter === "all" || item.decision === filter) &&
      item.title.toLocaleLowerCase().includes(query)
        ? [i]
        : [],
    );
  const changes = () =>
    drafts.flatMap((mode, i) =>
      mode !== items[i].mode ? [{ itemKey: items[i].itemKey, mode }] : [],
    );
  openDialog(
    entry.sheet_title + " · 업체 전체 상품 확인",
    `<div class="item-rule-toolbar"><div class="item-summary"><span>업체 전체 · 객실 ${groups.room.length} · 제외 ${groups.excluded.length} · 확인 필요 ${groups.review.length}</span><span id="itemDraftCount" aria-live="polite"></span></div><div class="item-summary">분류: ${esc(data.entries.filter(e => e.company_id === entry.company_id && !e.archived).map(e => e.sheet_title).join(" · "))}</div><div class="item-rule-filters"><div class="item-filter-options" role="group" aria-label="현재 판별 필터">${Object.entries(
      { all: "전체", review: "확인 필요", room: "객실", excluded: "제외" },
    )
      .map(
        ([value, label]) =>
          `<button data-item-filter="${value}" aria-pressed="${value === "all"}">${label} <span>${value === "all" ? items.length : groups[value].length}</span></button>`,
      )
      .join(
        "",
      )}</div><label class="item-rule-search">${icon("search")}<input id="itemRuleSearch" type="search" aria-label="상품명 검색" placeholder="상품명 검색"></label></div><div class="item-bulk-bar"><div class="item-selection"><label><input id="itemSelectVisible" type="checkbox">현재 목록 전체 선택</label><span id="itemSelectedCount" aria-live="polite">0개 선택</span></div><div class="item-bulk-actions" role="group" aria-label="선택 상품 일괄 변경">${Object.entries(
      modes,
    )
      .map(
        ([mode, label]) =>
          `<button data-item-bulk="${mode}" disabled>${icon(mode === "auto" ? "rotate-ccw" : mode === "include" ? "check" : "ban")}${label}</button>`,
      )
      .join(
        "",
      )}</div></div></div><div class="item-table-scroll"><table class="item-table"><thead><tr><th class="item-select-col" aria-label="일괄 처리 선택"></th><th>실제 상품명 / 현재 판별</th><th class="item-mode-col">처리 기준</th></tr></thead><tbody id="itemRuleRows"></tbody></table></div><p id="itemRuleEmpty" class="item-rule-empty" hidden>검색 조건에 맞는 상품이 없습니다.</p>`,
    [
      ["취소", () => $("#dialog").close()],
      [
        "다음 · 변경 목록 확인",
        async () => {
          const decisions = items.map((item, i) => ({...item,
            mode: drafts[i] === "auto" ? (item.decision === "room" ? "include" : item.decision === "excluded" ? "exclude" : "review") : drafts[i]}));
          if (decisions.some(item => item.mode === "review")) throw new Error("확인 필요 상품을 객실로 포함 또는 항상 제외로 선택해 주세요.");
          openReviewApproval(entry, decisions, dbRevision, rulesRevision);
        },
        true,
      ],
    ],
  );
  const updateControls = () => {
    const visible = visibleIndices();
    const count = visible.filter((i) => checked.has(i)).length;
    $("#itemSelectVisible").checked =
      !!visible.length && count === visible.length;
    $("#itemSelectVisible").indeterminate = count > 0 && count < visible.length;
    $("#itemSelectVisible").disabled = !visible.length;
    $("#itemSelectedCount").textContent = `${count}개 선택`;
    $$("[data-item-bulk]").forEach((button) => (button.disabled = !count));
    const changed = changes().length;
    $("#itemDraftCount").textContent = changed
      ? `저장 전 변경 ${changed}개`
      : "";
    $("#dialogActions .primary").disabled = data.busy;
    $$("[data-item-row]").forEach((row) => {
      const i = Number(row.dataset.itemRow);
      row.classList.toggle("item-rule-changed", drafts[i] !== items[i].mode);
      row.classList.toggle("item-rule-selected", checked.has(i));
    });
  };
  const renderItems = () => {
    const visible = visibleIndices();
    $("#itemRuleRows").innerHTML = visible
      .map((i) => {
        const item = items[i];
        return `<tr data-item-row="${i}"><td class="item-select-col"><input type="checkbox" data-item-select="${i}" aria-label="${esc(item.title)} 일괄 처리 선택" ${checked.has(i) ? "checked" : ""}></td><td class="item-name-cell"><strong>${esc(item.title)}</strong><div class="item-current"><span class="badge ${item.decision === "room" ? "ok" : item.decision === "review" ? "warn" : ""}">${labels[item.decision]}</span><small>${esc(item.reason)}</small></div></td><td class="item-mode-cell"><div class="item-rule-options" role="radiogroup" aria-label="${esc(item.title)} 처리 기준">${Object.entries(
          modes,
        )
          .map(
            ([mode, label]) =>
              `<label><input type="radio" name="item-rule-${i}" data-room-rule="${i}" value="${mode}" ${drafts[i] === mode ? "checked" : ""}><span>${label}</span></label>`,
          )
          .join("")}</div></td></tr>`;
      })
      .join("");
    $("#itemRuleEmpty").hidden = !!visible.length;
    updateControls();
  };
  $("#itemRuleRows").onchange = (event) => {
    const input = event.target;
    if (input.matches("[data-item-select]")) {
      const i = Number(input.dataset.itemSelect);
      if (input.checked) checked.add(i);
      else checked.delete(i);
    } else if (input.matches("[data-room-rule]")) {
      drafts[Number(input.dataset.roomRule)] = input.value;
    }
    updateControls();
  };
  $("#itemSelectVisible").onchange = (event) => {
    for (const i of visibleIndices()) {
      if (event.target.checked) checked.add(i);
      else checked.delete(i);
    }
    $$("[data-item-select]").forEach(
      (input) =>
        (input.checked = checked.has(Number(input.dataset.itemSelect))),
    );
    updateControls();
  };
  $$("[data-item-bulk]").forEach(
    (button) =>
      (button.onclick = () => {
        for (const i of checked) drafts[i] = button.dataset.itemBulk;
        $$("[data-room-rule]").forEach(
          (input) =>
            (input.checked =
              drafts[Number(input.dataset.roomRule)] === input.value),
        );
        updateControls();
      }),
  );
  $$("[data-item-filter]").forEach(
    (button) =>
      (button.onclick = () => {
        filter = button.dataset.itemFilter;
        // A filtered bulk action must not include hidden selections.
        checked.clear();
        $$("[data-item-filter]").forEach((el) =>
          el.setAttribute("aria-pressed", String(el === button)),
        );
        renderItems();
      }),
  );
  $("#itemRuleSearch").oninput = (event) => {
    query = event.target.value.trim().toLocaleLowerCase();
    checked.clear();
    renderItems();
  };
  renderItems();
}
function openReviewApproval(entry, decisions, revision, rulesRevision) {
  const members = data.entries.filter(e => e.company_id === entry.company_id && !e.archived);
  const pendingDrafts = members.filter(e => reviewDrafts.has(e.key));
  const included = decisions.filter(item => item.mode === "include");
  const normalize = value => value.replace(/[\s\u200b]/g, "");
  for (const item of included) {
    const owners = members.filter(e => e.rooms.some(room => normalize(room) === normalize(item.title)));
    item.target_key = members.length === 1 ? members[0].key : owners.length === 1 ? owners[0].key : "";
  }
  const archives = new Set();
  const targetRooms = e => included.filter(item => item.target_key === e.key).map(item => item.title);
  openDialog("변경 확인 · DB 반영", `<div class="approval-summary"><strong>${esc(entry.major || entry.sheet_title)}</strong><span>객실 ${included.length}개 · 제외 ${decisions.length - included.length}개</span></div>${pendingDrafts.length ? `<p class="warning-note">미저장 직접 입력 ${pendingDrafts.length}개 분류: ${esc(pendingDrafts.map(e => e.sheet_title).join(", "))}. 저장 시 아래 확인 목록으로 대체됩니다.</p>` : ""}${members.length > 1 ? `<div class="approval-assignments">${included.map((item, i) => `<div class="approval-assignment"><strong>${esc(item.title)}</strong><div class="item-rule-options" role="radiogroup" aria-label="${esc(item.title)} 저장 분류">${members.map(e => `<label><input type="radio" name="assign-${i}" data-assign="${i}" value="${e.key}" ${item.target_key === e.key ? "checked" : ""}><span>${esc(e.sheet_title)}</span></label>`).join("")}</div></div>`).join("")}</div>` : ""}<div id="approvalPreview"></div><div id="approvalWarning" class="warning-note" role="status"></div><label class="manual-authority"><input type="checkbox" id="approveConfirmed">위 변경 내용과 저장할 객실 목록을 확인했습니다</label>`, [
    ["취소", () => $("#dialog").close()],
    ["확인한 변경 저장 후 재수집", async () => {
      if (!$("#approveConfirmed").checked) throw new Error("저장할 변경 목록을 확인해 주세요.");
      const saved = await api("/api/approve-review", {
        key: entry.key, revision, rules_revision: rulesRevision, items_checked_at: entry.items_checked_at,
        decisions: decisions.map(({itemKey, mode, target_key}) => ({itemKey, mode, target_key})),
        archive_empty_keys: members.filter(e => !targetRooms(e).length && archives.has(e.key)).map(e => e.key),
        collect_after: !!data.cloud, date: $("#checkin").value,
      });
      $("#dialog").close();
      if (saved.queued) {
        pendingReview = {id: saved.job_id, keys: members.map(e => e.key)};
        await refresh(true);
        showView("changes");
        toast("확인한 변경의 DB 저장 및 해당 업체 재수집을 요청했습니다.");
      } else {
        members.forEach(e => { reviewDrafts.delete(e.key); reviewSelected.delete(e.key); });
        proposalSignature = "";
        await refresh(true);
        await start("collect", saved.keys);
      }
    }, true],
  ]);
  const renderPreview = () => {
    $("#approvalPreview").innerHTML = members.map(e => {
      const rooms = targetRooms(e), before = new Set(e.rooms.map(normalize)), after = new Set(rooms.map(normalize));
      const added = rooms.filter(room => !before.has(normalize(room))), removed = e.rooms.filter(room => !after.has(normalize(room)));
      return `<section class="approval-preview"><h3>${esc(e.sheet_title)} <span>${e.rooms.length}개 → ${rooms.length}개</span></h3><div class="change-grid"><label>현재 DB<textarea readonly aria-label="${esc(e.sheet_title)} 변경 전">${esc(e.rooms.join("\n"))}</textarea></label><label>확인 후 저장할 DB<textarea readonly aria-label="${esc(e.sheet_title)} 변경 후">${esc(rooms.join("\n"))}</textarea></label></div>${diffNotes({added, removed})}${!rooms.length ? `<label class="manual-authority"><input type="checkbox" data-archive-empty="${e.key}" ${archives.has(e.key) ? "checked" : ""}>객실이 없는 이 분류를 보관 처리합니다</label>` : ""}</section>`;
    }).join("");
    updateApproval();
  };
  const updateApproval = () => {
    const missing = included.filter(item => !item.target_key).length;
    const empty = members.filter(e => !targetRooms(e).length && !archives.has(e.key));
    const warning = !included.length ? "확정할 객실이 없습니다. 먼저 수집 결과를 확인해 주세요." : missing ? `${missing}개 객실의 저장 분류를 선택해 주세요.` : empty.length ? "객실이 없는 분류의 보관 여부를 확인해 주세요." : "";
    $("#approvalWarning").textContent = warning;
    $("#approvalWarning").hidden = !warning;
    $("#dialogActions .primary").disabled = data.busy || !!warning || !$("#approveConfirmed").checked;
  };
  $("#dialogBody").onchange = event => {
    const input = event.target;
    if (input.matches("[data-assign]")) {
      included[Number(input.dataset.assign)].target_key = input.value;
      $("#approveConfirmed").checked = false;
      renderPreview();
    } else if (input.matches("[data-archive-empty]")) {
      input.checked ? archives.add(input.dataset.archiveEmpty) : archives.delete(input.dataset.archiveEmpty);
      $("#approveConfirmed").checked = false;
      updateApproval();
    } else if (input.id === "approveConfirmed") updateApproval();
  };
  renderPreview();
}
function renderChanges() {
  const entries = visibleReviews();
  const signature = JSON.stringify([reviewFilter, $("#reviewSearch").value, data.revision, data.busy,
    entries.map(e => [e.key, e.proposal, e.changed, e.result?.status, e.items_checked_at, e.rules_revision])]);
  $("#proposalSummary").textContent = `확인 대상 ${reviewEntries().length}개 · 현재 표시 ${entries.length}개`;
  updateReviewControls();
  if (signature === proposalSignature) return;
  proposalSignature = signature;
  const opened = new Set($$("[data-review-detail][open]").map(el => el.dataset.reviewDetail));
  $("#changeList").innerHTML =
    entries
      .map((e) => {
        const p = e.proposal || {}, draft = reviewDrafts.get(e.key);
        const rooms = reviewRooms(e), items = reviewItems(e);
        const reason = !items.length ? "수집 정보 없음 · DB 유지" : e.room_authority === "manual" ? (e.changed && (!e.result || e.result.superseded) ? "수동 확정 · 재수집 대기" : "수동 목록 적용 중")
          : draft && draft.revision !== data.revision ? ($("#reviewManual").checked ? "내 수정 내용 우선 반영" : "DB 변경됨 · 편집 내용 재확인 필요")
          : p.error || (p.review?.length ? `상품 판별 ${p.review.length}개 필요` : p.split ? "분류별 객실 목록 확인"
          : proposalReady(e) ? "반영 가능" : e.changed ? "수정 후 재수집 필요" : "원본 확인 / 재점검 필요");
        return `<article class="review-row" data-proposal="${e.key}"><div class="review-heading"><input type="checkbox" data-apply="${e.key}" aria-label="${esc(e.sheet_title)} 변경 반영 선택" ${reviewSelected.has(e.key) ? "checked" : ""}><div><strong>${esc(e.major)} / ${esc(e.sheet_title)}</strong><small>${esc(e.company_id)} · DB ${e.rooms.length}개 · 수집 상품 ${items.length}개</small></div><span class="badge ${items.length && proposalReady(e) ? "ok" : "warn"}">${esc(reason)}</span></div><div class="review-row-actions"><button class="${items.length ? "primary" : ""}" ${items.length ? "data-review-items" : "data-review-rescan"}="${e.key}" ${data.busy ? "disabled" : ""}>${icon(items.length ? "list-checks" : "refresh-cw")}${items.length ? "수집된 객실 선택" : "이 업체 다시 확인"}</button><a class="text-link" href="https:\x2f\x2fm.place.naver.com/accommodation/${encodeURIComponent(e.company_id)}/room" target="_blank" rel="noopener">원본 ${icon("external-link")}</a></div><details data-review-detail="${e.key}" ${opened.has(e.key) || draft ? "open" : ""}><summary>기존 DB와 수집 결과</summary><div class="review-detail">${reviewComparison(e)}<details class="review-manual" ${draft ? "open" : ""}><summary>객실명 직접 입력${draft ? " · 저장 전 수정" : ""}</summary><label>직접 입력한 객실명<textarea data-proposed="${e.key}" aria-label="${esc(e.sheet_title)} 반영할 객실명">${esc(rooms)}</textarea></label><div class="review-edit-actions"><span data-draft-note="${e.key}">${draft ? "저장 전 수정" : ""}</span><button class="icon-button" data-reset-review="${e.key}" title="수정 취소">${icon("undo-2")}</button></div></details></div></details></article>`;
      })
      .join("") || empty("확인할 항목이 없습니다", "list-checks");
  $$("#changeList .review-row").forEach(row => {
    const entry = entries.find(e => e.key === row.dataset.proposal);
    row.querySelector(".review-row-actions").insertAdjacentHTML("afterbegin", `<button data-review-edit="${entry.key}" ${data.busy ? "disabled" : ""}>${icon("pencil")}객실·금액 수정</button>`);
    row.querySelector(".review-row-actions").insertAdjacentHTML("beforeend", `<button class="icon-button danger" data-review-delete="${entry.key}" title="목록에서 삭제" aria-label="${esc(entry.sheet_title)} 목록에서 삭제" ${data.busy ? "disabled" : ""}>${icon("trash-2")}</button>`);
  });
  $$("[data-reset-review]").forEach(button => {
    if (!$("#reviewManual").checked) return;
    const key = button.dataset.resetReview;
    const confirm = document.createElement("button");
    confirm.innerHTML = `${icon("check")}현재 DB 그대로 확정`;
    confirm.onclick = () => {
      const entry = data.entries.find(e => e.key === key);
      reviewDrafts.set(key, {rooms: entry.rooms.join("\n"), revision: data.revision});
      reviewSelected.add(key);
      $("#reviewManual").checked = true;
      proposalSignature = "";
      renderChanges(); icons();
    };
    button.before(confirm);
  });
  updateReviewControls();
}
function reviewEntries() {
  return data.entries.filter(e => !e.archived && (needsAttention(e) || reviewDrafts.has(e.key)));
}
function proposalReady(e) {
  const p = e.proposal;
  return !!(p?.changed && p.safe && !p.error && !p.review?.length && p.proposed?.length
    && p.revision === data.revision && p.parser_version === data.parser_version);
}
function visibleReviews() {
  const query = $("#reviewSearch").value.trim().toLocaleLowerCase();
  return reviewEntries().filter(e => (reviewFilter === "all" || proposalReady(e) === (reviewFilter === "ready"))
    && [e.major, e.sheet_title, e.company_id, ...e.rooms, ...reviewItems(e).map(item => item.title)].join(" ").toLocaleLowerCase().includes(query));
}
function reviewCanSave(e) {
  const draft = reviewDrafts.get(e.key);
  if ($("#reviewManual").checked) return !!reviewRooms(e).trim();
  return draft ? draft.revision === data.revision && !!draft.rooms.trim() : proposalReady(e);
}
function reviewRooms(e) {
  return reviewDrafts.get(e.key)?.rooms ?? (proposalReady(e) ? e.proposal.proposed.join("\n") : e.rooms.join("\n"));
}
function updateReviewControls() {
  const all = reviewEntries(), visible = visibleReviews();
  const selectedEntries = all.filter(e => reviewSelected.has(e.key));
  const count = visible.filter(e => reviewSelected.has(e.key)).length;
  $("#reviewSelectAll").checked = !!visible.length && count === visible.length;
  $("#reviewSelectAll").indeterminate = count > 0 && count < visible.length;
  $("#reviewSelectedCount").textContent = `${selectedEntries.length}개 선택 · 저장 전 수정 ${reviewDrafts.size}개`;
  $("#applyLabel").textContent = $("#reviewManual").checked ? "선택 강제 반영" : "확인한 변경 저장";
  $("#applyChanges").disabled = $("#applyAndCollect").disabled = data.busy || !selectedEntries.length || !selectedEntries.every(reviewCanSave);
  $("#reviewScan").disabled = $("#reviewCollect").disabled = data.busy || !selectedEntries.length || !!reviewDrafts.size;
  $("#reviewDelete").disabled = data.busy || !selectedEntries.length;
  $("#scanAll").disabled = data.busy || !all.length || !!reviewDrafts.size;
  $("#selectReady").disabled = data.busy || !visible.some(proposalReady);
  $$("[data-apply]").forEach(el => el.checked = reviewSelected.has(el.dataset.apply));
}
async function applyChanges(collect) {
  const keys = reviewEntries().filter(e => reviewSelected.has(e.key)).map(e => e.key);
  if (!keys.length) throw new Error("변경을 확인한 항목을 선택해 주세요.");
  const entries = data.entries.filter((e) => keys.includes(e.key));
  if (!entries.every(reviewCanSave)) throw new Error("직접 확인 항목은 객실명을 수정하거나 재점검해 주세요. DB가 변경된 편집은 수정 취소 후 다시 확인해 주세요.");
  const edits = entries.map((e) => ({
    ...e,
    rooms: reviewRooms(e),
    rooms_only: true,
    room_authority: $("#reviewManual").checked ? "manual" : "auto",
    from_proposal: !$("#reviewManual").checked && !reviewDrafts.has(e.key),
  }));
  const saved = await api("/api/apply", {
    revision: data.revision,
    edits,
    force_manual: $("#reviewManual").checked,
    ...(collect && data.cloud ? {collect_after: true, date: $("#checkin").value} : {}),
  });
  if (saved.queued) {
    pendingReview = {id: saved.job_id, keys};
    await refresh(true);
    toast(`${keys.length}개 항목 저장 및 재수집을 한 번에 요청했습니다.`);
    return;
  }
  keys.forEach(key => { reviewDrafts.delete(key); reviewSelected.delete(key); });
  proposalSignature = "";
  selected = new Set(saved.keys);
  await refresh(true);
  toast(saved.keys.length + "개 항목 일괄 반영 및 백업 완료");
  if (collect) await start("collect", saved.keys);
}
function renderDiscoveries() {
  const signature = JSON.stringify(data.discoveries);
  if (signature === discoverySignature) return;
  discoverySignature = signature;
  $("#discoveryCount").textContent =
    "검색 후보 " + data.discoveries.length + "개";
  $("#discoveryList").innerHTML = data.discoveries.length
    ? `<div class="discovery-table"><table><thead><tr><th></th><th>업체 / 중분류</th><th>객실</th><th>예약 확인</th><th>원본</th></tr></thead><tbody>${data.discoveries.map((d, i) => `<tr><td><input type="checkbox" data-discovery="${i}" ${d.existing || !d.verified ? "disabled" : ""} aria-label="${esc(d.name)} 추가 선택"></td><td><span class="row-title">${esc(d.name)}</span><input data-discovery-title="${i}" value="${esc(d.name.slice(0, 31))}" maxlength="31" aria-label="중분류 이름"><div class="row-sub">${esc(d.company_id)} · ${esc(d.region)}</div></td><td>${d.rooms.length}</td><td><span class="badge ${d.verified ? "ok" : "warn"}">${d.existing ? "기존 DB 업체" : esc(d.status)}</span></td><td><a href="${esc(d.url)}" target="_blank" rel="noopener" title="네이버 원본 확인">${icon("external-link")}</a></td></tr>`).join("")}</tbody></table></div>`
    : empty("지역 검색 또는 링크 확인 결과가 표시됩니다", "map-pin");
}
async function addDiscoveries() {
  const indices = $$("[data-discovery]:checked").map((el) =>
    Number(el.dataset.discovery),
  );
  if (!indices.length)
    throw new Error("예약 링크와 객실이 확인된 신규 업체를 선택해 주세요.");
  const edits = indices.map((i) => {
    const d = data.discoveries[i];
    return {
      company_id: d.company_id,
      major: d.name,
      sheet_title: $(`[data-discovery-title="${i}"]`).value,
      region: d.region,
      rooms: d.rooms,
      price: "",
    };
  });
  const saved = await api("/api/apply", { revision: data.revision, edits });
  selected = new Set(saved.keys);
  activeKey = saved.keys[0];
  await refresh(true);
  showView("catalog");
  renderInspector();
  toast(saved.keys.length + "개 업체를 DB에 추가했습니다.");
}
function renderJobs() {
  const signature = JSON.stringify(data.jobs);
  if (signature === jobsSignature) return;
  jobsSignature = signature;
  $("#jobCount").textContent = data.jobs.length + "개 작업";
  $("#jobList").innerHTML =
    data.jobs
      .map(
        (j, i) =>
          `<div class="job-row" data-job="${i}" tabindex="0"><div>${formatTime(j.started_at)}<small>체크인 ${esc(j.date)}</small></div><div><strong>${kinds[j.kind] || j.kind}</strong><small>처리 ${j.completed}/${j.total} · 정상 ${j.normal} · 확인 필요 ${j.issues}</small></div><span class="badge ${j.state === "completed" ? "ok" : j.state === "failed" ? "error" : "warn"}">${states[j.state]}</span><span class="muted">${j.finished_at ? j.finished_at.slice(11, 16) : "진행 중"}</span></div>`,
      )
      .join("") || empty("작업 이력이 없습니다", "clipboard-list");
}
function showLog(job = data.job) {
  if (job.remote_log) {
    return api('/api/job', {id: job.id}).then(showLog).catch(error => toast(error.message, true));
  }
  openDialog(
    kinds[job.kind] || "활동 로그",
    (job.logs || [])
      .map(
        (l) =>
          `<div class="log-line"><time>${esc(l.time.slice(11))}</time>${esc(l.text)}</div>`,
      )
      .join("") || empty("기록 없음"),
    [["닫기", () => $("#dialog").close()]],
  );
}
$$("nav button").forEach(
  (b) =>
    (b.onclick = () => {
      if (drawerDirty) {
        toast("입력한 내용을 먼저 저장해 주세요.");
        return;
      }
      showView(b.dataset.view);
    }),
);
$("#search").oninput = render;
$("#summarySearch").oninput = () => { renderSummary(); icons(); };
$("#addCamp").onclick = guarded(() => openAddCamp());
$("#collectIssues").onclick = guarded(() => start("collect", data.entries.filter(e => !e.archived && needsAttention(e)).map(e => e.key)));
$("#businessRows").onclick = guarded(async event => {
  const edit = event.target.closest("[data-summary-edit]");
  if (edit) editModal([data.entries.find(e => e.key === edit.dataset.summaryEdit)]);
  const history = event.target.closest("[data-summary-history]");
  if (history) await openTrend(history.dataset.summaryHistory);
  const copy = event.target.closest("[data-summary-copy]");
  if (copy) editModal([data.entries.find(e => e.key === copy.dataset.summaryCopy)], true);
  const remove = event.target.closest("[data-summary-delete]");
  if (remove) confirmListRemoval([remove.dataset.summaryDelete]);
  const collect = event.target.closest("[data-summary-collect]");
  if (collect) await start("collect", [collect.dataset.summaryCollect]);
});
$("#statusFilter").onchange = () => {
  selected.clear();
  render();
};
$("#checkin").onchange = () => {
  tableSignature = "";
  render();
};
$("#reload").onclick = guarded(async () => {
  if (drawerDirty) throw new Error("입력한 내용을 먼저 저장해 주세요.");
  await refresh(true);
  renderInspector();
  toast("새로고침 완료");
});
$("#selectAll").onchange = (e) => {
  for (const entry of visibleEntries())
    e.target.checked ? selected.add(entry.key) : selected.delete(entry.key);
  render();
};
$("#catalogRows").onclick = (e) => {
  const check = e.target.closest("[data-select]");
  if (check) {
    check.checked
      ? selected.add(check.dataset.select)
      : selected.delete(check.dataset.select);
    render();
    return;
  }
  const row = e.target.closest("[data-key]");
  if (row) {
    if (drawerDirty) {
      toast("입력한 내용을 먼저 저장해 주세요.");
      return;
    }
    activeKey = row.dataset.key;
    inspectorTab = "rooms";
    renderInspector();
    render();
  }
};
$("#catalogRows").onkeydown = (e) => {
  if (e.key === "Enter" && e.target.matches("tr")) e.target.click();
};
$("#collectAll").onclick = guarded(() => start("collect", null, true));
$("#collectSelected").onclick = guarded(() => start("collect", [...selected]));
$("#scanSelected").onclick = guarded(() => start("scan", [...selected]));
$("#scanAll").onclick = guarded(() => start("scan", reviewEntries().map(e => e.key)));
$("#stop").onclick = guarded(async () => {
  const r = await api("/api/stop", {});
  toast(r.message);
});
$("#bulkEdit").onclick = () =>
  editModal(data.entries.filter((e) => selected.has(e.key)));
$("#applyChanges").onclick = guarded(() => applyChanges(false));
$("#applyAndCollect").onclick = guarded(() => applyChanges(true));
$("#reviewSearch").oninput = () => { renderChanges(); icons(); };
$("#reviewManual").onchange = () => { proposalSignature = ""; renderChanges(); icons(); };
$$("[data-review-filter]").forEach(button => button.onclick = () => {
  reviewFilter = button.dataset.reviewFilter;
  $$("[data-review-filter]").forEach(b => b.setAttribute("aria-pressed", String(b === button)));
  renderChanges(); icons();
});
$("#reviewSelectAll").onchange = event => {
  visibleReviews().forEach(e => event.target.checked ? reviewSelected.add(e.key) : reviewSelected.delete(e.key));
  updateReviewControls();
};
$("#selectReady").onclick = () => {
  reviewSelected.clear();
  visibleReviews().filter(proposalReady).forEach(e => reviewSelected.add(e.key));
  updateReviewControls();
};
$("#reviewScan").onclick = guarded(() => start("scan", [...reviewSelected]));
$("#reviewCollect").onclick = guarded(() => start("collect", [...reviewSelected]));
$("#reviewDelete").onclick = guarded(() => confirmListRemoval(reviewEntries().filter(e => reviewSelected.has(e.key)).map(e => e.key)));
$("#changeList").onchange = event => {
  const key = event.target.dataset.apply;
  if (key) {
    event.target.checked ? reviewSelected.add(key) : reviewSelected.delete(key);
    updateReviewControls();
  }
};
$("#changeList").oninput = event => {
  const key = event.target.dataset.proposed;
  if (!key) return;
  const old = reviewDrafts.get(key);
  reviewDrafts.set(key, {rooms: event.target.value, revision: old?.revision ?? data.revision});
  reviewSelected.add(key);
  $(`[data-draft-note="${key}"]`).textContent = "저장 전 수정";
  updateReviewControls();
};
$("#changeList").addEventListener(
  "click",
  guarded(async (e) => {
    const edit = e.target.closest("[data-review-edit]");
    if (edit) editModal([data.entries.find(entry => entry.key === edit.dataset.reviewEdit)]);
    const button = e.target.closest("[data-review-items]");
    if (button) {
      openItemRules(button.dataset.reviewItems);
    }
    const rescan = e.target.closest("[data-review-rescan]");
    if (rescan) await start("scan", [rescan.dataset.reviewRescan]);
    const remove = e.target.closest("[data-review-delete]");
    if (remove) confirmListRemoval([remove.dataset.reviewDelete]);
    const reset = e.target.closest("[data-reset-review]");
    if (reset) {
      reviewDrafts.delete(reset.dataset.resetReview);
      proposalSignature = "";
      renderChanges(); icons();
    }
  }),
);
$("#archive").onclick = guarded(() => confirmListRemoval([...selected], $("#statusFilter").value !== "archived"));
$("#discoverForm").onsubmit = guarded(async (e) => {
  e.preventDefault();
  await start("discover", null, false, {
    region: $("#region").value,
    keyword: $("#keyword").value,
    limit: Number($("#limit").value),
  });
});
$("#verifyLinks").onclick = guarded(() =>
  start("verify", null, false, {
    links: $("#importLinks").value,
    region: $("#region").value,
  }),
);
$("#region").oninput = () =>
  ($("#naverSearch").href =
    "https:\x2f\x2fmap.naver.com/p/search/" +
    encodeURIComponent($("#region").value + " " + $("#keyword").value));
$("#addDiscoveries").onclick = guarded(addDiscoveries);
$("#jobList").onclick = (e) => {
  const row = e.target.closest("[data-job]");
  if (row) showLog(data.jobs[Number(row.dataset.job)]);
};
$("#jobList").onkeydown = (e) => {
  if (e.key === "Enter") e.target.click();
};
$("#showLog").onclick = () => showLog();
$("#closeDialog").onclick = () => $("#dialog").close();
window.addEventListener("beforeunload", (e) => {
  if (drawerDirty || reviewDrafts.size) {
    e.preventDefault();
    e.returnValue = "";
  }
});
window.addEventListener("resize", () => {
  if (inspectorTab === "history" && current()) loadHistory(activeKey);
  if (activeTrend && $("#dialog").open) renderTrend();
});
$("#dialog").addEventListener("close", () => { activeTrend = null; pendingAdd = null; });
icons();
refresh();
async function poll() {
  if (!document.hidden) await refresh();
  setTimeout(poll, window.bookingCloudApi ? (data?.busy ? 12000 : 45000) : 2200);
}
setTimeout(poll, window.bookingCloudApi ? 12000 : 2200);
