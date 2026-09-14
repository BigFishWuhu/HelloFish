const $ = (selector) => document.querySelector(selector);

const controls = {
    dateMode: $("#date-mode"),
    recentDays: $("#recent-days"),
    startDate: $("#start-date"),
    endDate: $("#end-date"),
    minWealth: $("#min-wealth"),
    includeUnknown: $("#include-unknown"),
    gender: $("#gender"),
    minFriends: $("#min-friends"),
    roomId: $("#room-id"),
    userId: $("#user-id"),
    username: $("#username"),
    unit: $("#unit"),
    sort: $("#sort"),
};

const state = {page: 1, pageSize: 50, total: 0, records: []};
const storageKey = "hellofish-contribution-filters-v1";
const exportStorageKey = "hellofish-contribution-export-columns-v1";
let toastTimer;

function localIsoDate(value = new Date()) {
    const offset = value.getTimezoneOffset() * 60_000;
    return new Date(value.getTime() - offset).toISOString().slice(0, 10);
}

function restoreFilters() {
    const today = localIsoDate();
    controls.startDate.value = today;
    controls.endDate.value = today;
    try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || "{}");
        for (const [
            name,
            control,
        ] of Object.entries(controls)) {
            if (!(name in saved)) continue;
            if (control.type === "checkbox") control.checked = Boolean(saved[name]);
            else control.value = String(saved[name]);
        }
    } catch (_) {
        localStorage.removeItem(storageKey);
    }
    updateDateControls();
}

function persistFilters() {
    const values = {};
    for (const [
        name,
        control,
    ] of Object.entries(controls)) {
        values[name] = control.type === "checkbox" ? control.checked : control.value;
    }
    localStorage.setItem(storageKey, JSON.stringify(values));
}

function updateDateControls() {
    $("#recent-control").classList.toggle("hidden", controls.dateMode.value !== "recent");
    $("#custom-control").classList.toggle("hidden", controls.dateMode.value !== "custom");
}

function formatChineseNumber(value, suffix) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "???";
    const display = Number.isInteger(number) ? String(number) : number.toFixed(1).replace(/\.0$/, "");
    if (number <= 10_000) return `${display}${suffix}`;
    const wan = Math.floor(number / 10_000);
    const remainder = number - wan * 10_000;
    if (remainder === 0) return `${wan}万${suffix}`;
    const tail = Number.isInteger(remainder) ? String(remainder) : remainder.toFixed(1).replace(/\.0$/, "");
    return `${wan}万${tail}${suffix}`;
}

function formatThreshold(contribution) {
    if (contribution === null || contribution === undefined) return "???";
    if (controls.unit.value === "contribution") {
        return formatChineseNumber(contribution, " 贡献值");
    }
    return formatChineseNumber(Number(contribution) / 10, " 元");
}

function escapeText(value) {
    return value === null || value === undefined || value === "" ? "—" : String(value);
}

function showToast(message) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("visible"), 1800);
}

async function copyText(value, label) {
    try {
        await navigator.clipboard.writeText(value);
    } catch (_) {
        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.setAttribute("readonly", "");
        textarea.className = "clipboard-fallback";
        document.body.append(textarea);
        textarea.select();
        const copied = document.execCommand("copy");
        textarea.remove();
        if (!copied) throw new Error("copy failed");
    }
    showToast(`已复制${label}：${value}`);
}

function copyButton(value, label, text, className = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `copy-value ${className}`.trim();
    button.textContent = text;
    if (value === null || value === undefined || value === "") {
        button.disabled = true;
        return button;
    }
    button.title = `点击复制${label}`;
    button.setAttribute("aria-label", `${text}，点击复制${label}`);
    button.addEventListener("click", async () => {
        try {
            await copyText(String(value), label);
        } catch (_) {
            showToast(`复制${label}失败，请手动选择复制`);
        }
    });
    return button;
}

function renderRecords() {
    const body = $("#records-body");
    body.replaceChildren();
    for (const record of state.records) {
        const row = document.createElement("tr");
        const scanned = String(record.scanned_at || "").replace("T", " ");
        const room = document.createElement("div");
        room.className = "identity";
        room.append(document.createTextNode(escapeText(record.room_name)));
        const roomId = document.createElement("small");
        roomId.textContent = `ID ${record.room_id}`;
        room.append(roomId);
        const user = document.createElement("div");
        user.className = "identity";
        user.append(
            copyButton(record.username, "用户名", escapeText(record.username), "copy-primary"),
            copyButton(record.user_id, "用户 ID", `ID ${record.user_id}`, "copy-secondary"),
        );

        const values = [
            scanned,
            room,
            record.rank,
            user,
            record.gender,
            record.ip,
            record.close_friend_count,
            record.wealth_level ?? "???",
            formatThreshold(record.wealth_min_contribution),
            record.charm_level,
        ];
        values.forEach((value, index) => {
            const cell = document.createElement("td");
            if (value instanceof Node) cell.append(value);
            else cell.textContent = escapeText(value);
            if (index === 7) cell.className = "wealth";
            row.append(cell);
        });
        body.append(row);
    }
    $("#empty-state").classList.toggle("hidden", state.records.length !== 0);
    $("#threshold-heading").textContent = controls.unit.value === "yuan" ? "等级最低金额" : "等级最低贡献值";
}

function queryParams() {
    const params = new URLSearchParams({
        date_mode: controls.dateMode.value,
        days: controls.recentDays.value || "7",
        start_date: controls.startDate.value,
        end_date: controls.endDate.value,
        min_wealth_level: controls.minWealth.value,
        include_unknown: String(controls.includeUnknown.checked),
        gender: controls.gender.value,
        min_close_friend_count: controls.minFriends.value,
        room_id: controls.roomId.value.trim(),
        user_id: controls.userId.value.trim(),
        username: controls.username.value.trim(),
        sort: controls.sort.value,
        page: String(state.page),
        page_size: String(state.pageSize),
    });
    return params.toString();
}

function exportColumnControls() {
    return [...document.querySelectorAll("[data-export-column]")];
}

function restoreExportColumns() {
    let saved;
    try {
        saved = JSON.parse(localStorage.getItem(exportStorageKey) || "null");
    } catch (_) {
        localStorage.removeItem(exportStorageKey);
    }
    if (!Array.isArray(saved)) return;
    const selected = new Set(saved);
    exportColumnControls().forEach((control) => {
        control.checked = selected.has(control.dataset.exportColumn);
    });
}

function openExport() {
    restoreExportColumns();
    $("#export-status").textContent = "";
    $("#export-dialog").showModal();
}

function startExport() {
    const columns = exportColumnControls()
        .filter((control) => control.checked)
        .map((control) => control.dataset.exportColumn);
    if (columns.length === 0) {
        $("#export-status").textContent = "请至少选择一列。";
        return;
    }
    persistFilters();
    localStorage.setItem(exportStorageKey, JSON.stringify(columns));
    const params = new URLSearchParams(queryParams());
    params.delete("page");
    params.delete("page_size");
    params.set("columns", columns.join(","));
    const download = document.createElement("a");
    download.href = `/api/export?${params.toString()}`;
    download.hidden = true;
    document.body.append(download);
    download.click();
    download.remove();
    $("#export-dialog").close();
    showToast(`正在导出 ${columns.length} 列，筛选条件已保留`);
}

async function loadRecords() {
    persistFilters();
    $("#status").textContent = "正在读取本地数据…";
    try {
        const response = await fetch(`/api/records?${queryParams()}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "读取失败");
        state.records = data.records;
        state.total = data.total;
        state.page = data.page;
        renderRecords();
        const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
        $("#record-total").textContent = String(state.total);
        $("#date-summary").textContent =
            data.start_date === data.end_date ? data.start_date : `${data.start_date} — ${data.end_date}`;
        $("#page-summary").textContent = `第 ${state.page} / ${pages} 页`;
        $("#previous-page").disabled = state.page <= 1;
        $("#next-page").disabled = state.page >= pages;
        $("#status").textContent = `更新于 ${new Date().toLocaleTimeString()}`;
    } catch (error) {
        state.records = [];
        renderRecords();
        $("#status").textContent = `读取失败：${error.message}`;
    }
}

function parseIdList(value) {
    return [
        ...new Set(
            value
                .split(/[\s,，;；]+/)
                .map((item) => item.trim())
                .filter(Boolean),
        ),
    ];
}

async function openSettings() {
    $("#settings-status").textContent = "正在读取…";
    $("#settings-dialog").showModal();
    try {
        const response = await fetch("/api/settings");
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "读取失败");
        $("#hidden-room-ids").value = data.hidden_room_ids.join("\n");
        $("#hidden-user-ids").value = data.hidden_user_ids.join("\n");
        $("#settings-status").textContent = "";
    } catch (error) {
        $("#settings-status").textContent = `读取失败：${error.message}`;
    }
}

async function saveSettings() {
    const button = $("#save-settings");
    button.disabled = true;
    $("#settings-status").textContent = "正在保存…";
    try {
        const response = await fetch("/api/settings", {
            method: "PUT",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                hidden_room_ids: parseIdList($("#hidden-room-ids").value),
                hidden_user_ids: parseIdList($("#hidden-user-ids").value),
            }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "保存失败");
        $("#settings-status").textContent =
            `已保存：隐藏 ${data.hidden_room_ids.length} 个厅、${data.hidden_user_ids.length} 个用户`;
        state.page = 1;
        await loadRecords();
        setTimeout(() => $("#settings-dialog").close(), 500);
    } catch (error) {
        $("#settings-status").textContent = `保存失败：${error.message}`;
    } finally {
        button.disabled = false;
    }
}

controls.dateMode.addEventListener("change", updateDateControls);
controls.unit.addEventListener("change", () => {
    persistFilters();
    renderRecords();
});
$("#apply-filters").addEventListener("click", () => {
    state.page = 1;
    loadRecords();
});
[
    controls.minWealth,
    controls.minFriends,
    controls.roomId,
    controls.userId,
    controls.username,
].forEach((control) => {
    control.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            state.page = 1;
            loadRecords();
        }
    });
});
$("#previous-page").addEventListener("click", () => {
    state.page -= 1;
    loadRecords();
});
$("#next-page").addEventListener("click", () => {
    state.page += 1;
    loadRecords();
});
$("#open-settings").addEventListener("click", openSettings);
$("#save-settings").addEventListener("click", saveSettings);
$("#open-export").addEventListener("click", openExport);
$("#start-export").addEventListener("click", startExport);
$("#select-all-columns").addEventListener("click", () => {
    exportColumnControls().forEach((control) => (control.checked = true));
});
$("#clear-all-columns").addEventListener("click", () => {
    exportColumnControls().forEach((control) => (control.checked = false));
});

restoreFilters();
loadRecords();
