const refreshButton = document.querySelector("#health-refresh");
const overview = document.querySelector("#health-overview");
const overviewTitle = document.querySelector("#health-overview-title");
const overviewDetail = document.querySelector("#health-overview-detail");
const carrierGrid = document.querySelector("#carrier-health-grid");
const automationGrid = document.querySelector("#automation-health-grid");
const errorBox = document.querySelector("#health-error");

const STATUS_LABELS = {
  ok: "ปกติ",
  warning: "ควรตรวจสอบ",
  error: "มีปัญหา",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatCheckedAt(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "ไม่พบเวลา";
  return new Intl.DateTimeFormat("th-TH", {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(parsed);
}

function healthCard(item) {
  const latency = Number.isFinite(item.latency_ms)
    ? `<span>ตอบสนอง ${escapeHtml(item.latency_ms)} ms</span>`
    : "";
  return `
    <article class="health-card ${escapeHtml(item.status)}">
      <div class="health-card-top">
        <span class="health-icon" aria-hidden="true"></span>
        <span class="health-badge">${escapeHtml(STATUS_LABELS[item.status] || "ไม่ทราบ")}</span>
      </div>
      <h3>${escapeHtml(item.name)}</h3>
      <strong>${escapeHtml(item.summary)}</strong>
      <p>${escapeHtml(item.detail)}</p>
      <div class="health-card-meta">
        <span>ตรวจ ${escapeHtml(formatCheckedAt(item.checked_at))}</span>
        ${latency}
      </div>
    </article>
  `;
}

function render(payload) {
  const checks = Array.isArray(payload.checks) ? payload.checks : [];
  const carriers = checks.filter((item) => item.group === "ขนส่ง");
  const automations = checks.filter((item) => item.group !== "ขนส่ง");
  carrierGrid.innerHTML = carriers.map(healthCard).join("");
  automationGrid.innerHTML = automations.map(healthCard).join("");

  overview.className = `health-overview ${payload.overall || "error"}`;
  const counts = payload.counts || {};
  if (payload.overall === "ok") {
    overviewTitle.textContent = "ทุกระบบทำงานปกติ";
  } else if (payload.overall === "warning") {
    overviewTitle.textContent = "มีรายการที่ควรตรวจสอบ";
  } else {
    overviewTitle.textContent = "พบระบบที่ต้องแก้ไข";
  }
  overviewDetail.textContent =
    `ปกติ ${counts.ok || 0} · ควรตรวจสอบ ${counts.warning || 0} · ` +
    `มีปัญหา ${counts.error || 0} · ตรวจล่าสุด ${formatCheckedAt(payload.checked_at)}`;
}

async function loadHealth() {
  refreshButton.disabled = true;
  refreshButton.textContent = "กำลังตรวจ…";
  errorBox.classList.add("hidden");
  try {
    const response = await fetch("/api/admin/health", {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "ตรวจระบบไม่สำเร็จ");
    render(payload);
  } catch (error) {
    errorBox.textContent = error.message || "ตรวจระบบไม่สำเร็จ กรุณาลองใหม่";
    errorBox.classList.remove("hidden");
    overview.className = "health-overview error";
    overviewTitle.textContent = "โหลดสถานะระบบไม่ได้";
    overviewDetail.textContent = "กรุณากดตรวจใหม่อีกครั้ง";
  } finally {
    refreshButton.disabled = false;
    refreshButton.textContent = "ตรวจใหม่";
  }
}

refreshButton.addEventListener("click", loadHealth);
loadHealth();
window.setInterval(loadHealth, 60_000);
