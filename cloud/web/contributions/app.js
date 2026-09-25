const $ = (selector) => document.querySelector(selector);

const controls = {
    dateMode: $("#date-mode"),
    recentDays: $("#recent-days"),
    startDate: $("#start-time"),
    endDate: $("#end-time"),
    minWealth: $("#min-wealth"),
    includeUnknown: $("#include-unknown"),
    gender: $("#gender"),
    minFriends: $("#min-friends"),
    minEstimated: $("#min-estimated"),
    roomId: $("#room-id"),
    userId: $("#user-id"),
    username: $("#username"),
    unit: $("#unit"),
    sort: $("#sort"),
};

const defaultColumnOrder = [
    "username", "rooms", "scanned_at", "gender", "ip",
    "close_friend_count", "estimated_contribution_total",
    "wealth_level", "charm_level", "account_assessment",
];
const columnLabels = {
    username: "用户名",
    rooms: "出现厅",
    scanned_at: "最近记录",
    gender: "性别",
    ip: "IP 属地",
    close_friend_count: "挚友",
    estimated_contribution_total: "合计推测金额下限",
    wealth_level: "财富等级",
    charm_level: "魅力等级",
    account_assessment: "账号判断",
};
const exportColumnsBySummaryColumn = {
    username: ["username"],
    rooms: ["room_name"],
    scanned_at: ["scanned_at", "scan_date"],
    gender: ["gender"],
    ip: ["ip"],
    close_friend_count: ["close_friend_count"],
    estimated_contribution_total: ["estimated_contribution_total"],
    wealth_level: ["wealth_level", "wealth_min_contribution"],
    charm_level: ["charm_level", "charm_min_value"],
    account_assessment: ["account_assessment"],
};
const state = {
    page: 1,
    pageSize: 50,
    total: 0,
    records: [],
    columnOrder: [...defaultColumnOrder],
};
const storageKey = "hellofish-contribution-filters-v1";
const exportStorageKey = "hellofish-contribution-export-columns-v1";
const columnOrderStorageKey = "hellofish-contribution-column-order-v1";
const copiedUserStorageKey = "hellofish-copied-user-ids-v1";
const loginStorageKey = "hellofish-cloud-login-v1";
const copiedUserTtlMs = 12 * 60 * 60 * 1000;
const copiedUsers = new Map();
let toastTimer;
let draftColumnOrder = [];
let setupMode = false;
let currentUsername = "";

function restoreLoginCredentials() {
    try {
        const saved = JSON.parse(localStorage.getItem(loginStorageKey) || "null");
        if (!saved || typeof saved.username !== "string" || typeof saved.password !== "string") {
            return;
        }
        $("#login-username").value = saved.username;
        $("#login-password").value = saved.password;
        $("#remember-password").checked = true;
    } catch (_) {
        localStorage.removeItem(loginStorageKey);
    }
}

function persistLoginCredentials(username, password) {
    try {
        if ($("#remember-password").checked) {
            localStorage.setItem(loginStorageKey, JSON.stringify({username, password}));
        } else {
            localStorage.removeItem(loginStorageKey);
        }
    } catch (_) {
        // Login should still work when local storage is unavailable.
    }
}

function updateRememberedPassword(username, password) {
    if (!$("#remember-password").checked) return;
    try {
        const saved = JSON.parse(localStorage.getItem(loginStorageKey) || "null");
        if (saved && saved.username === username) {
            localStorage.setItem(loginStorageKey, JSON.stringify({username, password}));
        }
    } catch (_) {
        localStorage.removeItem(loginStorageKey);
    }
}

function localIsoDate(value = new Date()) {
    const parts = new Intl.DateTimeFormat("en-US", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
    }).formatToParts(value);
    const values = Object.fromEntries(parts.map(({type, value: part}) => [type, part]));
    return `${values.year}-${values.month}-${values.day}`;
}

function localIsoMinute(value = new Date()) {
    const parts = new Intl.DateTimeFormat("en-US", {
        timeZone: "Asia/Shanghai",
        hourCycle: "h23",
        hour: "2-digit",
        minute: "2-digit",
    }).formatToParts(value);
    const values = Object.fromEntries(parts.map(({type, value: part}) => [type, part]));
    return `${localIsoDate(value)}T${values.hour}:${values.minute}`;
}

function restoreCopiedUsers() {
    try {
        const saved = JSON.parse(localStorage.getItem(copiedUserStorageKey) || "{}");
        const now = Date.now();
        if (saved && typeof saved === "object" && !Array.isArray(saved)) {
            for (const [userId, copiedAt] of Object.entries(saved)) {
                if (Number.isFinite(copiedAt) && now - copiedAt < copiedUserTtlMs) {
                    copiedUsers.set(userId, copiedAt);
                }
            }
        }
        localStorage.setItem(copiedUserStorageKey, JSON.stringify(Object.fromEntries(copiedUsers)));
    } catch (_) {
        copiedUsers.clear();
    }
}

function wasUserCopied(userId) {
    const copiedAt = copiedUsers.get(userId);
    return Number.isFinite(copiedAt) && Date.now() - copiedAt < copiedUserTtlMs;
}

function rememberCopiedUser(userId) {
    copiedUsers.set(userId, Date.now());
    try {
        localStorage.setItem(copiedUserStorageKey, JSON.stringify(Object.fromEntries(copiedUsers)));
    } catch (_) {
        // Copying should still work when local storage is unavailable.
    }
}

function restoreFilters() {
    const today = localIsoDate();
    controls.startDate.value = `${today}T00:00`;
    controls.endDate.value = localIsoMinute();
    try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || "{}");
        for (const [
            name,
            control,
        ] of Object.entries(controls)) {
            if (!(name in saved)) continue;
            if (control.type === "checkbox") control.checked = Boolean(saved[name]);
            else {
                const value = String(saved[name]);
                if (control.type === "datetime-local" && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
                    control.value = `${value}T${name === "endDate" ? "23:59" : "00:00"}`;
                } else control.value = value;
            }
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

function normalizeColumnOrder(value) {
    const requested = Array.isArray(value)
        ? value.filter((column, index) => columnLabels[column] && value.indexOf(column) === index)
        : [];
    return [...requested, ...defaultColumnOrder.filter((column) => !requested.includes(column))];
}

function restoreColumnOrder() {
    try {
        state.columnOrder = normalizeColumnOrder(
            JSON.parse(localStorage.getItem(columnOrderStorageKey) || "null"),
        );
    } catch (_) {
        localStorage.removeItem(columnOrderStorageKey);
        state.columnOrder = [...defaultColumnOrder];
    }
}

function renderColumnOrderEditor() {
    const list = $("#column-order-list");
    list.replaceChildren();
    draftColumnOrder.forEach((column, index) => {
        const item = document.createElement("div");
        item.className = "column-order-item";
        item.dataset.column = column;
        const position = document.createElement("strong");
        position.textContent = String(index + 1);
        const label = document.createElement("span");
        label.textContent = column === "estimated_contribution_total"
            ? (controls.unit.value === "yuan" ? "合计推测金额下限" : "合计推测贡献值下限")
            : columnLabels[column];
        const up = document.createElement("button");
        up.type = "button";
        up.dataset.move = "up";
        up.textContent = "↑";
        up.disabled = index === 0;
        up.setAttribute("aria-label", `上移${label.textContent}`);
        const down = document.createElement("button");
        down.type = "button";
        down.dataset.move = "down";
        down.textContent = "↓";
        down.disabled = index === draftColumnOrder.length - 1;
        down.setAttribute("aria-label", `下移${label.textContent}`);
        item.append(position, label, up, down);
        list.append(item);
    });
}

function openColumnOrder() {
    draftColumnOrder = [...state.columnOrder];
    renderColumnOrderEditor();
    $("#column-order-dialog").showModal();
}

function saveColumnOrder() {
    state.columnOrder = normalizeColumnOrder(draftColumnOrder);
    localStorage.setItem(columnOrderStorageKey, JSON.stringify(state.columnOrder));
    renderRecords();
    $("#column-order-dialog").close();
    showToast("主列表及导出列顺序已更新");
}

function updateDateControls() {
    $("#recent-control").classList.toggle("hidden", controls.dateMode.value !== "recent");
    $("#custom-control").classList.toggle("hidden", controls.dateMode.value !== "custom");
}

function formatChineseNumber(value, suffix) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "???";
    const display = Number.isInteger(number) ? String(number) : number.toFixed(1).replace(/\.0$/, "");
    if (number < 10_000) return `${display}${suffix}`;
    const wan = (number / 10_000).toFixed(2).replace(/\.?0+$/, "");
    return `${wan}万${suffix}`;
}

function formatThreshold(contribution) {
    if (contribution === null || contribution === undefined) return "???";
    if (controls.unit.value === "contribution") {
        return formatChineseNumber(contribution, " 贡献值");
    }
    return formatChineseNumber(Number(contribution) / 10, " 元");
}

function formatCharmThreshold(charmValue) {
    if (charmValue === null || charmValue === undefined) return "???";
    if (controls.unit.value === "contribution") {
        return formatChineseNumber(charmValue, " 魅力值");
    }
    return formatChineseNumber(Number(charmValue) / 10, " 元");
}

function formatContributionValue(contribution) {
    return formatThreshold(contribution);
}

function formatEstimatedValue(contribution) {
    return contribution === null || contribution === undefined
        ? "???"
        : `≥ ${formatContributionValue(contribution)}`;
}

function formatScanTime(value) {
    if (value === null || value === undefined || value === "") return "—";
    const text = String(value);
    const match = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/);
    return match ? `${match[1]} ${match[2]}` : text.replace("T", " ");
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

function userCopyButton(record) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy-value user-copy";

    const username = document.createElement("span");
    username.className = "copy-primary";
    username.textContent = escapeText(record.username);
    const userId = document.createElement("span");
    userId.className = "copy-secondary";
    userId.textContent = `ID ${record.user_id}`;
    button.append(username, userId);

    if (record.user_id === null || record.user_id === undefined || record.user_id === "") {
        button.disabled = true;
        return button;
    }
    button.title = "点击复制用户 ID";
    button.setAttribute("aria-label", `${escapeText(record.username)}，用户 ID ${record.user_id}，点击复制用户 ID`);
    button.classList.toggle("copied-user", wasUserCopied(String(record.user_id)));
    button.addEventListener("click", async () => {
        try {
            await copyText(String(record.user_id), "用户 ID");
            rememberCopiedUser(String(record.user_id));
            button.classList.add("copied-user");
        } catch (_) {
            showToast("复制用户 ID 失败，请手动选择复制");
        }
    });
    return button;
}

function renderRecords() {
    const body = $("#records-body");
    body.replaceChildren();
    const header = $("#summary-columns");
    header.replaceChildren();
    state.columnOrder.forEach((column) => {
        const heading = document.createElement("th");
        heading.dataset.column = column;
        heading.textContent = column === "estimated_contribution_total"
            ? (controls.unit.value === "yuan" ? "合计推测金额下限" : "合计推测贡献值下限")
            : columnLabels[column];
        header.append(heading);
    });
    $("#min-estimated-label").textContent = controls.unit.value === "yuan"
        ? "合计推测金额下限至少（元）"
        : "合计推测贡献值下限至少";
    for (const record of state.records) {
        const appearances = Array.isArray(record.appearances) && record.appearances.length
            ? record.appearances
            : [record];
        const row = document.createElement("tr");
        row.className = "user-summary-row";
        const user = userCopyButton(record);

        const roomSummary = document.createElement("button");
        roomSummary.type = "button";
        roomSummary.className = "detail-toggle";
        roomSummary.setAttribute("aria-expanded", "false");
        const uniqueRooms = new Set(appearances.map((item) => String(item.room_id ?? "")));
        roomSummary.textContent = `查看 ${record.room_count ?? uniqueRooms.size} 个厅 · ${record.appearance_count ?? appearances.length} 条记录`;

        const wealth = document.createElement("span");
        wealth.textContent = record.wealth_level ?? "???";
        const wealthMinimum = document.createElement("small");
        wealthMinimum.textContent = `（${formatThreshold(record.wealth_min_contribution)}）`;
        wealth.append(wealthMinimum);

        const charm = document.createElement("span");
        charm.textContent = record.charm_level ?? "???";
        const charmMinimum = document.createElement("small");
        charmMinimum.textContent = `（${formatCharmThreshold(record.charm_min_value)}）`;
        charm.append(charmMinimum);

        const assessment = document.createElement("span");
        assessment.className = "assessment";
        assessment.textContent = record.account_assessment ?? "—";
        if (record.account_assessment === "疑似排挡账号") {
            assessment.classList.add("suspected");
        } else if (record.account_assessment === "高概率真实玩家") {
            assessment.classList.add("likely-real");
        }

        const values = {
            username: user,
            rooms: roomSummary,
            scanned_at: formatScanTime(record.scanned_at),
            gender: record.gender,
            ip: record.ip,
            close_friend_count: record.close_friend_count,
            estimated_contribution_total: formatEstimatedValue(record.estimated_contribution_total),
            wealth_level: wealth,
            charm_level: charm,
            account_assessment: assessment,
        };
        state.columnOrder.forEach((column) => {
            const value = values[column];
            const cell = document.createElement("td");
            if (value instanceof Node) cell.append(value);
            else cell.textContent = escapeText(value);
            cell.dataset.column = column;
            if (column === "estimated_contribution_total") cell.className = "estimated-total";
            if (column === "wealth_level") cell.className = "wealth";
            if (column === "charm_level") cell.className = "charm";
            row.append(cell);
        });

        const detailRow = document.createElement("tr");
        detailRow.className = "user-detail-row hidden";
        const detailCell = document.createElement("td");
        detailCell.colSpan = state.columnOrder.length;
        const detailWrap = document.createElement("div");
        detailWrap.className = "appearance-details";
        const detailTable = document.createElement("table");
        detailTable.className = "appearance-table";
        const detailHead = document.createElement("thead");
        const headingRow = document.createElement("tr");
        [
            "日期 / 时间",
            "厅",
            "排名",
            controls.unit.value === "yuan" ? "距前一名金额" : "距前一名贡献值",
            controls.unit.value === "yuan" ? "推测金额下限" : "推测贡献值下限",
            "性别",
            "IP 属地",
            "挚友",
            "财富等级",
            "魅力等级",
            "账号判断",
        ].forEach((label) => {
            const heading = document.createElement("th");
            heading.textContent = label;
            headingRow.append(heading);
        });
        detailHead.append(headingRow);
        const detailBody = document.createElement("tbody");
        appearances.forEach((appearance) => {
            const appearanceRow = document.createElement("tr");
            const room = document.createElement("div");
            room.className = "identity";
            room.append(document.createTextNode(escapeText(appearance.room_name)));
            const roomId = document.createElement("small");
            roomId.textContent = `ID ${appearance.room_id}`;
            room.append(roomId);
            [
                formatScanTime(appearance.scanned_at),
                room,
                appearance.rank,
                formatContributionValue(appearance.contribution_gap),
                formatEstimatedValue(appearance.estimated_contribution_value),
                appearance.gender,
                appearance.ip,
                appearance.close_friend_count,
                `${appearance.wealth_level ?? "???"}（${formatThreshold(appearance.wealth_min_contribution)}）`,
                `${appearance.charm_level ?? "???"}（${formatCharmThreshold(appearance.charm_min_value)}）`,
                appearance.account_assessment,
            ].forEach((value) => {
                const cell = document.createElement("td");
                if (value instanceof Node) cell.append(value);
                else cell.textContent = escapeText(value);
                appearanceRow.append(cell);
            });
            detailBody.append(appearanceRow);
        });
        detailTable.append(detailHead, detailBody);
        detailWrap.append(detailTable);
        detailCell.append(detailWrap);
        detailRow.append(detailCell);
        roomSummary.addEventListener("click", () => {
            const expanded = roomSummary.getAttribute("aria-expanded") === "true";
            roomSummary.setAttribute("aria-expanded", String(!expanded));
            roomSummary.textContent = expanded
                ? `查看 ${record.room_count ?? uniqueRooms.size} 个厅 · ${record.appearance_count ?? appearances.length} 条记录`
                : "收起各厅详情";
            detailRow.classList.toggle("hidden", expanded);
        });
        body.append(row, detailRow);
    }
    $("#empty-state").classList.toggle("hidden", state.records.length !== 0);
}

function queryParams() {
    const minimumEstimated = controls.minEstimated.value === ""
        ? ""
        : String(Math.ceil(Number(controls.minEstimated.value) * (
            controls.unit.value === "yuan" ? 10 : 1
        )));
    const params = new URLSearchParams({
        date_mode: controls.dateMode.value,
        days: controls.recentDays.value || "7",
        start_time: controls.startDate.value,
        end_time: controls.endDate.value,
        min_wealth_level: controls.minWealth.value,
        include_unknown: String(controls.includeUnknown.checked),
        gender: controls.gender.value,
        min_close_friend_count: controls.minFriends.value,
        min_estimated_contribution_total: minimumEstimated,
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

function reorderExportColumns() {
    const grid = $(".column-grid");
    const controlsByColumn = new Map(
        exportColumnControls().map((control) => [control.dataset.exportColumn, control]),
    );
    const orderedColumns = state.columnOrder.flatMap(
        (column) => exportColumnsBySummaryColumn[column] || [],
    );
    const placed = new Set();
    for (const column of orderedColumns) {
        const control = controlsByColumn.get(column);
        if (control) {
            grid.append(control.closest("label"));
            placed.add(column);
        }
    }
    for (const [column, control] of controlsByColumn) {
        if (!placed.has(column)) grid.append(control.closest("label"));
    }
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
    reorderExportColumns();
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
    params.set("column_order", state.columnOrder.join(","));
    const download = document.createElement("a");
    download.href = `/api/export?${params.toString()}`;
    download.hidden = true;
    document.body.append(download);
    download.click();
    download.remove();
    $("#export-dialog").close();
    showToast(`正在导出 ${columns.length} 列，筛选条件已保留`);
}

async function startHtmlExport() {
    const button = $("#start-html-export");
    persistFilters();
    const params = new URLSearchParams(queryParams());
    params.delete("page");
    params.delete("page_size");
    params.set("column_order", state.columnOrder.join(","));
    button.disabled = true;
    $("#export-status").textContent = "正在生成静态 HTML…";
    try {
        const response = await fetch(`/api/export.html?${params.toString()}`);
        if (response.status === 401) {
            showLogin();
            throw new Error("登录已失效，请重新登录");
        }
        if (!response.ok) {
            let message = "导出失败";
            try {
                message = (await response.json()).error || message;
            } catch (_) {
                // Keep the generic message when the response is not JSON.
            }
            throw new Error(message);
        }
        const blobUrl = URL.createObjectURL(await response.blob());
        const download = document.createElement("a");
        const disposition = response.headers.get("Content-Disposition") || "";
        const encodedFilename = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
        const filename = encodedFilename
            ? decodeURIComponent(encodedFilename)
            : disposition.match(/filename="?([^";]+)"?/i)?.[1];
        download.href = blobUrl;
        download.download = filename || `${new Date().toISOString().slice(0, 10)}-财富密码.html`;
        download.hidden = true;
        document.body.append(download);
        download.click();
        download.remove();
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
        $("#export-dialog").close();
        showToast("静态 HTML 已生成");
    } catch (error) {
        $("#export-status").textContent = `导出失败：${error.message}`;
    } finally {
        button.disabled = false;
    }
}

async function loadRecords() {
    persistFilters();
    $("#status").textContent = "正在读取云端数据…";
    try {
        const response = await fetch(`/api/records?${queryParams()}`);
        const data = await response.json();
        if (response.status === 401) {
            showLogin();
            throw new Error("请先登录");
        }
        if (!response.ok) throw new Error(data.error || "读取失败");
        state.records = data.records;
        state.total = data.total;
        state.page = data.page;
        renderRecords();
        const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
        $("#record-total").textContent = String(state.total);
        $("#date-summary").textContent =
            `${data.start_time.replace("T", " ")} — ${data.end_time.replace("T", " ")}`;
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

function showLogin(firstSetup = false) {
    setupMode = firstSetup;
    $("#login-title").textContent = firstSetup ? "设置云端账号" : "登录云端记录";
    $("#login-subtitle").textContent = firstSetup
        ? "首次访问请设置账号和密码，完成后即可使用云端记录。"
        : "请输入账号密码访问你的贡献记录。";
    $("#login-confirm-field").classList.toggle("hidden", !firstSetup);
    $("#login-password").autocomplete = firstSetup ? "new-password" : "current-password";
    $("#login-password-confirm").required = firstSetup;
    $("#login-submit").textContent = firstSetup ? "完成设置" : "登录";
    $("#login-panel").classList.add("visible");
    document.body.classList.add("logged-out");
}

function hideLogin() {
    $("#login-panel").classList.remove("visible");
    document.body.classList.remove("logged-out");
}

async function checkAuth() {
    const response = await fetch("/api/auth/me");
    const data = await response.json();
    currentUsername = data.authenticated ? String(data.username || "") : "";
    if (!data.authenticated) showLogin(Boolean(data.setup_required));
    else hideLogin();
    return Boolean(data.authenticated);
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

function openPasswordDialog() {
    ["current-password", "new-password", "password-confirm"].forEach((id) => {
        $(id).value = "";
    });
    $("password-status").textContent = "";
    $("password-dialog").showModal();
}

async function savePassword() {
    const button = $("save-password");
    const status = $("password-status");
    const newPassword = $("new-password").value;
    const confirmation = $("password-confirm").value;
    if (newPassword !== confirmation) {
        status.textContent = "两次输入的新密码不一致";
        return;
    }
    button.disabled = true;
    status.textContent = "正在保存…";
    try {
        const response = await fetch("/api/auth/change-password", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                current_password: $("current-password").value,
                new_password: newPassword,
                password_confirmation: confirmation,
            }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "修改密码失败");
        updateRememberedPassword(currentUsername, newPassword);
        status.textContent = "密码已修改";
        setTimeout(() => $("password-dialog").close(), 500);
    } catch (error) {
        status.textContent = error.message;
    } finally {
        button.disabled = false;
    }
}

controls.dateMode.addEventListener("change", updateDateControls);
controls.unit.addEventListener("change", () => {
    const previousUnit = controls.unit.dataset.previousUnit || "yuan";
    if (controls.minEstimated.value !== "" && previousUnit !== controls.unit.value) {
        const value = Number(controls.minEstimated.value);
        controls.minEstimated.value = String(
            controls.unit.value === "contribution" ? value * 10 : value / 10,
        );
    }
    controls.unit.dataset.previousUnit = controls.unit.value;
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
    controls.minEstimated,
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
$("#open-column-order").addEventListener("click", openColumnOrder);
$("#save-column-order").addEventListener("click", saveColumnOrder);
$("#reset-column-order").addEventListener("click", () => {
    draftColumnOrder = [...defaultColumnOrder];
    renderColumnOrderEditor();
});
$("#column-order-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-move]");
    if (!button) return;
    const column = button.closest("[data-column]").dataset.column;
    const index = draftColumnOrder.indexOf(column);
    const target = button.dataset.move === "up" ? index - 1 : index + 1;
    if (target < 0 || target >= draftColumnOrder.length) return;
    [draftColumnOrder[index], draftColumnOrder[target]] = [
        draftColumnOrder[target],
        draftColumnOrder[index],
    ];
    renderColumnOrderEditor();
});
$("#open-password").addEventListener("click", openPasswordDialog);
$("#save-password").addEventListener("click", savePassword);
$("#open-export").addEventListener("click", openExport);
$("#start-export").addEventListener("click", startExport);
$("#start-html-export").addEventListener("click", startHtmlExport);
$("#select-all-columns").addEventListener("click", () => {
    exportColumnControls().forEach((control) => (control.checked = true));
});
$("#clear-all-columns").addEventListener("click", () => {
    exportColumnControls().forEach((control) => (control.checked = false));
});
$("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = $("#login-status");
    status.textContent = setupMode ? "正在保存账号…" : "正在登录…";
    try {
        const payload = {
            username: $("#login-username").value,
            password: $("#login-password").value,
        };
        if (setupMode) payload.password_confirmation = $("#login-password-confirm").value;
        const response = await fetch(setupMode ? "/api/auth/setup" : "/api/auth/login", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || (setupMode ? "设置失败" : "登录失败"));
        currentUsername = String(data.username || payload.username).trim();
        persistLoginCredentials(currentUsername, payload.password);
        status.textContent = "";
        hideLogin();
        await loadRecords();
    } catch (error) {
        status.textContent = error.message;
    }
});
$("#logout").addEventListener("click", async () => {
    await fetch("/api/auth/logout", {method: "POST"});
    currentUsername = "";
    showLogin();
});

restoreFilters();
controls.unit.dataset.previousUnit = controls.unit.value;
restoreColumnOrder();
restoreCopiedUsers();
restoreLoginCredentials();
checkAuth().then((authenticated) => {
    if (authenticated) loadRecords();
}).catch(() => {
    showLogin();
    $("#login-status").textContent = "无法连接云端服务";
});
