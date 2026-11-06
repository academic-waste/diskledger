"use strict";

const byId = (id) => document.getElementById(id);
const colors = ["#c3aa7b", "#d0cbc2", "#a6a39b", "#8a877f", "#777875", "#686b70"];
const pageSize = 25;
const selectionLimit = 200;
const integer = new Intl.NumberFormat("zh-CN");
const date = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
const dateTime = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const collator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });
const selected = new Set();
let snapshot = null;
let rootsById = new Map();
let filesById = new Map();
let rootId = "";
let projectKey = "";
const expandedSources = new Set();
let sort = { field: "allocated", direction: "descending" };
let page = 0;
let pageFiles = [];
let scanning = false;
let scanFailed = false;
let mutating = false;
let trashFiles = [];
let previewFile = null;

function element(tag, className, value) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (value !== undefined) item.textContent = value;
  return item;
}

function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const use = document.createElementNS(ns, "use");
  svg.setAttribute("aria-hidden", "true");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}

function iconButton(name, label, className = "") {
  const button = element("button", "icon-button " + className);
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.append(icon(name));
  return button;
}

function sizeParts(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const amount = Math.max(0, bytes || 0);
  const index = Math.min(units.length - 1, amount > 0 ? Math.floor(Math.log10(amount) / 3) : 0);
  const scaled = amount / (1000 ** index);
  return [
    index === 0 ? integer.format(scaled) : scaled.toLocaleString("zh-CN", { maximumFractionDigits: scaled < 10 ? 2 : 1 }),
    units[index],
  ];
}

function sizeText(bytes) {
  return sizeParts(bytes).join(" ");
}

function notice(message = "", type = "") {
  const banner = byId("notice");
  banner.className = "notice " + type;
  banner.hidden = !message;
  banner.replaceChildren(...(message ? [icon(type ? "alert" : "info"), element("span", "", message)] : []));
}

function feedback(id, message = "", isError = false) {
  const target = byId(id);
  target.hidden = !message;
  target.textContent = message;
  target.classList.toggle("error", isError);
}

async function request(path, method = "GET", data) {
  const options = { method, cache: "no-store" };
  if (method !== "GET") options.headers = { "X-Storage-Manager": "1" };
  if (data !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("无法连接本地服务。请检查启动窗口是否仍在运行。");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
  return payload;
}

function createSourceButton(root, className, jump = false) {
  const button = element("button", className);
  button.type = "button";
  button.dataset.sourceId = root.id;
  button.style.setProperty("--segment", root.color);
  button.addEventListener("click", () => setRoot(root.id, "", jump));
  return button;
}

function renderSources() {
  const navigation = document.createDocumentFragment();
  for (const root of rootsById.values()) {
    if (root.kind === "linked") continue;
    const button = createSourceButton(root, "nav-button");
    button.title = root.error ? `${root.path} · ${root.error}` : root.path;
    button.append(element("span", "source-marker"), element("span", "", root.label));
    if (root.error || root.skipped) {
      const alert = icon("alert");
      alert.classList.add("root-error");
      button.append(alert);
    }
    navigation.append(button);
  }
  byId("source-nav").replaceChildren(navigation);
}

function usageBar(value, scale, className) {
  const track = element("span", className);
  const fill = element("span");
  fill.style.width = `${value / (scale || 1) * 100}%`;
  track.setAttribute("aria-hidden", "true");
  track.append(fill);
  return track;
}

function renderRanking() {
  const sources = rootId ? [rootsById.get(rootId)] : [...rootsById.values()];
  const ranked = sources.flatMap((root) => (root.projects.length && (root.kind !== "linked" || rootId) ? root.projects : [{ ...root, key: "" }])
    .map((project) => ({ project, root })))
    .filter(({ project }) => project.allocated > 0)
    .sort((a, b) => b.project.allocated - a.project.allocated);
  const rows = ranked.slice(0, 5).map(({ project, root }, index) => {
    const item = element("li");
    const button = element("button", "ranking-item");
    button.type = "button";
    button.title = `${project.label} · ${project.path}`;
    button.addEventListener("click", () => setRoot(root.id, project.key, true));
    const labels = element("span", "ranking-labels");
    labels.append(element("span", "ranking-label", project.label),
      element("span", "ranking-source", [project.parentLabel || (project.key ? root.label : root.kind === "linked" ? "附属项目" : ""), project.detail].filter(Boolean).join(" · ")));
    button.append(element("span", "ranking-index", String(index + 1).padStart(2, "0")), labels,
      usageBar(project.allocated, ranked[0].project.allocated, "ranking-track"),
      element("span", "ranking-size", sizeText(project.allocated)));
    item.append(button);
    return item;
  });
  if (!rows.length) rows.push(element("li", "ranking-empty", snapshot ? "这个范围内还没有可排行的项目。" : "正在读取本地项目…"));
  byId("project-ranking").replaceChildren(...rows);
  byId("ranking-caption").textContent = ranked.length > 5 ? `前 5 / ${integer.format(ranked.length)} 项` : "按磁盘占用排序";
}

function projectButton(root, project) {
  const button = element("button", "project-child");
  button.type = "button";
  button.dataset.projectRoot = root.id;
  button.dataset.projectKey = project.key;
  button.title = project.path;
  button.addEventListener("click", () => setRoot(root.id, project.key, true));
  const label = element("span", "tree-labels");
  label.append(element("span", "tree-name", project.label));
  const detail = [project.parentLabel, project.detail].filter(Boolean).join(" · ");
  if (detail) label.append(element("small", "tree-detail", detail));
  button.append(icon("folder"), label, usageBar(project.allocated, root.allocated, "tree-track"),
    element("span", "tree-count", `${integer.format(project.count)} 个文件`),
    element("span", "tree-size", sizeText(project.allocated)), icon("right"));
  return button;
}

function renderProjectTree() {
  const roots = [...rootsById.values()];
  const sections = [roots.filter((root) => root.kind !== "linked"), roots.filter((root) => root.kind === "linked")];
  const projectCount = roots.reduce((count, root) => count + root.projects.length, 0);
  const otherProjects = snapshot.otherProjects || [];
  const tree = document.createDocumentFragment();
  for (const [index, group] of sections.entries()) {
    if (!group.length && (index === 0 || !otherProjects.length)) continue;
    const section = element("div", "tree-section");
    const entries = element("div", "tree-section-list");
    section.append(element("h3", "tree-section-label", index === 0 ? "产物来源" : "附属项目"), entries);
    for (const root of group.sort((a, b) => b.allocated - a.allocated)) {
      const entry = element("div", "tree-entry");
      const header = element("div", "tree-source");
      const expand = iconButton("right", `展开或收起 ${root.label}`, "tree-expand");
      const children = element("div", "project-children");
      children.id = `children-${root.id}`;
      expand.setAttribute("aria-controls", children.id);
      expand.disabled = !root.projects.length;
      const setOpen = (open) => {
        expand.setAttribute("aria-expanded", String(open));
        children.hidden = !open;
      };
      setOpen(expandedSources.has(root.id) && root.projects.length > 0);
      expand.addEventListener("click", () => {
        const open = children.hidden;
        if (open) expandedSources.add(root.id);
        else expandedSources.delete(root.id);
        setOpen(open);
      });
      const source = createSourceButton(root, "tree-source-button", true);
      source.title = root.path;
      const labels = element("span", "tree-labels");
      labels.append(element("span", "tree-name", root.label));
      if (root.kind === "linked") labels.append(element("small", "tree-detail", root.projects.map((project) => project.label).join(" · ") || "导出目录"));
      source.append(labels, usageBar(root.allocated, snapshot.totals.allocated, "tree-track"),
        element("span", "tree-count", root.error ? "无法读取" : root.projects.length ? `${integer.format(root.projects.length)} 个下属项` : `${integer.format(root.count)} 个文件`),
        element("span", "tree-size", root.error && !root.count ? "—" : sizeText(root.allocated)));
      header.append(expand, source);
      if (root.projects.length) children.append(...root.projects.map((project) => projectButton(root, project)));
      entry.append(header, children);
      entries.append(entry);
    }
    if (index === 1 && otherProjects.length) {
      const more = element("details", "other-projects");
      more.append(element("summary", "", `其他已登记项目 · ${integer.format(otherProjects.length)}`));
      const list = element("ul");
      for (const label of otherProjects) {
        const item = element("li");
        item.append(element("span", "tree-name", label), element("span", "tree-detail", "暂无独立导出扫描"));
        list.append(item);
      }
      more.append(list);
      entries.append(more);
    }
    tree.append(section);
  }
  if (!roots.length) tree.append(element("p", "ranking-empty", "还没有扫描目录。"));
  byId("project-tree").replaceChildren(tree);
  byId("project-count").textContent = `${integer.format(roots.length)} 个来源 · ${integer.format(projectCount)} 个文件组`;
}

function renderScope() {
  const root = rootsById.get(rootId);
  const project = root?.projects.find((item) => item.key === projectKey);
  byId("all-roots").classList.toggle("is-current", !root);
  byId("all-roots").setAttribute("aria-pressed", String(!root));
  for (const button of document.querySelectorAll("[data-source-id]")) {
    const active = button.dataset.sourceId === rootId && (!project || button.classList.contains("nav-button"));
    button.classList.toggle("is-current", active);
    button.setAttribute("aria-pressed", String(active));
  }
  for (const button of document.querySelectorAll("[data-project-root]")) {
    const active = button.dataset.projectRoot === rootId && button.dataset.projectKey === projectKey;
    button.setAttribute("aria-pressed", String(active));
  }
  byId("clear-project").hidden = !root;
  byId("clear-project").lastElementChild.textContent = project ? "清除项目筛选" : "查看全部来源";
  byId("breadcrumb-root").textContent = [root?.label, project?.label].filter(Boolean).join(" / ") || "存储总览";
  byId("hero-scope").textContent = root ? `${project?.label || root.label} · 磁盘占用` : "已扫描目录的磁盘占用";
  const unavailable = root?.error && !root.count;
  const totals = unavailable ? null : project || root || snapshot?.totals;
  const [value, unit] = totals ? sizeParts(totals.allocated) : ["—", ""];
  byId("total-value").textContent = value;
  byId("total-unit").textContent = unit;
  byId("total-files").textContent = totals ? `${integer.format(totals.count)} 个文件` : "— 个文件";
  const folderCount = [...rootsById.values()].filter((item) => !item.error || item.count > 0).length;
  byId("total-folders").textContent = !snapshot ? "— 个来源" : project ? root.label : root ? `${integer.format(root.projects.length)} 个下属项` : `${integer.format(folderCount)} 个来源`;

  const issues = root?.error || (root?.skipped ? `有 ${integer.format(root.skipped)} 项无法读取。` : "");
  const details = root ? `${project?.label ? `${project.label} · ` : ""}${project?.path || root.path}${issues ? ` · ${issues}` : ""}` : "";
  byId("list-summary").textContent = details || (snapshot ? "列出大小或磁盘占用至少 1 MiB 的文件，最多 5,000 项。" : "正在读取扫描结果…");
  byId("list-summary").classList.toggle("has-issue", Boolean(issues));
  byId("list-summary").title = details;
}

function renderDisk() {
  const disk = snapshot?.disk;
  byId("disk-summary").hidden = !(disk?.total > 0 && disk.free >= 0);
  if (byId("disk-summary").hidden) return;
  const used = Math.max(0, Math.min(100, Math.round((disk.total - disk.free) / disk.total * 100)));
  const meter = byId("disk-meter");
  meter.style.setProperty("--used", used + "%");
  meter.setAttribute("aria-valuenow", String(used));
  byId("disk-text").textContent = `剩余 ${sizeText(disk.free)} / ${sizeText(disk.total)}`;
}

function setRoot(id, key = "", jump = false) {
  const changed = id !== rootId || key !== projectKey;
  rootId = id;
  projectKey = key;
  if (changed) {
    page = 0;
    selected.clear();
    if (jump) {
      byId("search").value = "";
      byId("minimum-size").value = "0";
    }
    renderScope();
    renderRanking();
    renderFiles();
  }
  if (jump) {
    const title = byId("inventory-title");
    title.tabIndex = -1;
    title.focus({ preventScroll: true });
    byId("file-inventory").scrollIntoView({ block: "start" });
  }
}

function setControls() {
  byId("scan").disabled = scanning || mutating;
  document.body.classList.toggle("is-scanning", scanning);
  byId("scan").lastElementChild.textContent = scanning ? "扫描中" : "重新扫描";
  byId("scan-time").textContent = scanning ? "正在读取本地目录…" : snapshot ? `扫描于 ${dateTime.format(snapshot.scannedAt * 1000)}` : "尚未扫描";
  for (const id of ["roots-close", "root-path", "trash-close", "trash-cancel", "trash-submit"]) byId(id).disabled = mutating;
  byId("add-root").querySelector("button").disabled = mutating || scanning;
  for (const button of byId("root-list").querySelectorAll("button")) button.disabled = mutating || scanning;
  updateSelection();
}

async function scan() {
  if (scanning) return false;
  scanning = true;
  scanFailed = false;
  selected.clear();
  setControls();
  if (!snapshot) renderFiles();
  try {
    const updated = await request("/api/scan");
    if (!Array.isArray(updated.roots) || !Array.isArray(updated.files) || !updated.totals || !Number.isFinite(updated.scannedAt)) {
      throw new Error("本地服务返回了无法识别的扫描结果。");
    }
    snapshot = updated;
    rootsById = new Map(updated.roots.map((root, index) => [root.id, { ...root, projects: root.projects || [], color: colors[index % colors.length] }]));
    filesById = new Map(updated.files.map((file) => [file.id, file]));
    if (!rootsById.has(rootId)) rootId = "";
    if (!rootsById.get(rootId)?.projects.some((item) => item.key === projectKey)) projectKey = "";
    renderSources();
    renderProjectTree();
    renderScope();
    renderRanking();
    renderDisk();
    renderRootList();
    return true;
  } catch (error) {
    scanFailed = true;
    notice(snapshot ? `重新扫描失败，当前显示上次的结果。${error.message}` : error.message, "error");
    return false;
  } finally {
    scanning = false;
    setControls();
    renderFiles();
  }
}

function fileType(name) {
  const extension = name.split(".").pop().toLowerCase();
  if (["jpg", "jpeg", "png", "webp", "gif", "avif", "heic", "tiff", "svg", "psd"].includes(extension)) return "image";
  if (["mp4", "mov", "mkv", "avi", "webm", "m4v"].includes(extension)) return "video";
  if (["wav", "mp3", "aac", "m4a", "flac", "ogg"].includes(extension)) return "audio";
  if (["zip", "gz", "7z", "rar", "tar", "dmg"].includes(extension)) return "archive";
  return "file";
}

function clearPreviewMedia() {
  const media = byId("preview-stage").querySelector("img, video");
  if (!media) return;
  media.remove();
  if (media instanceof HTMLVideoElement) {
    media.pause();
    media.removeAttribute("src");
    media.load();
  } else {
    media.removeAttribute("src");
  }
}

function openPreview(file) {
  const dialog = byId("preview-dialog");
  const stage = byId("preview-stage");
  const status = byId("preview-status");
  const open = byId("preview-open");
  const isVideo = file.previewKind === "video";
  clearPreviewMedia();
  previewFile = file;
  open.hidden = file.name.toLowerCase().endsWith(".svg");
  open.disabled = false;
  open.classList.remove("primary");
  open.classList.add("secondary");
  open.lastElementChild.textContent = isVideo ? "用系统播放器打开" : "用系统应用打开";
  byId("preview-title").textContent = file.name;
  byId("preview-meta").textContent = [isVideo ? "视频" : "图片", sizeText(file.size), file.mtime ? date.format(file.mtime * 1000) : ""].filter(Boolean).join(" · ");
  byId("preview-path").textContent = file.path;
  feedback("preview-feedback");
  status.classList.remove("has-error");
  status.textContent = "正在读取本地文件…";
  status.hidden = false;

  const media = element(isVideo ? "video" : "img", "preview-media");
  media.hidden = true;
  if (isVideo) {
    media.controls = true;
    media.playsInline = true;
    media.preload = "metadata";
    media.setAttribute("aria-label", file.name);
  } else {
    media.alt = file.name;
  }
  media.addEventListener(isVideo ? "loadedmetadata" : "load", () => {
    if (!dialog.open || media.parentElement !== stage) return;
    status.hidden = true;
    media.hidden = false;
  }, { once: true });
  media.addEventListener("error", () => {
    if (!dialog.open || media.parentElement !== stage) return;
    clearPreviewMedia();
    status.textContent = open.hidden
      ? "无法预览此文件。可以复制路径，在 Finder 中打开。如果文件已移动或修改，请重新扫描。"
      : "浏览器无法预览此文件。部分格式或编码需要用 macOS 应用打开。如果文件已移动或修改，请重新扫描。";
    status.classList.add("has-error");
    status.hidden = false;
    open.classList.replace("secondary", "primary");
  }, { once: true });
  stage.append(media);
  dialog.showModal();
  media.src = `/api/preview/${encodeURIComponent(file.id)}`;
}

function renderRow(file) {
  const row = element("tr");
  row.dataset.fileId = file.id;
  const checkCell = element("td", "check-column");
  const checkbox = element("input");
  checkbox.type = "checkbox";
  checkbox.setAttribute("aria-label", `选择 ${file.name}`);
  checkbox.addEventListener("change", () => {
    if (!checkbox.checked) selected.delete(file.id);
    else if (selected.size < selectionLimit) selected.add(file.id);
    updateSelection();
  });
  checkCell.append(checkbox);
  const nameCell = element("td", "file-column");
  const canPreview = file.previewKind === "image" || file.previewKind === "video";
  const fileCell = element(canPreview ? "button" : "div", canPreview ? "file-cell file-preview" : "file-cell");
  const fileBadge = element("span", "file-type");
  fileBadge.dataset.type = fileType(file.name);
  fileBadge.append(icon(fileBadge.dataset.type));
  const label = element("span", "file-label");
  const name = element("span", "file-name", file.name);
  name.title = file.name;
  const source = rootsById.get(file.rootId);
  const project = source?.projects.find((item) => item.key === file.projectKey);
  const prefix = project ? [project.parentLabel || source.label, project.label].join(" / ") : source?.label || "本地";
  const relative = project ? file.relativePath.slice(project.key.length + 1) : file.relativePath;
  const displayPath = `${prefix} / ${relative || file.name}`;
  const path = element("span", "file-path", displayPath);
  path.title = file.path;
  label.append(name, path);
  fileCell.append(fileBadge, label);
  if (canPreview) {
    fileCell.type = "button";
    fileCell.setAttribute("aria-haspopup", "dialog");
    fileCell.setAttribute("aria-label", `预览${file.previewKind === "video" ? "视频" : "图片"} · ${file.name}`);
    const hint = element("span", "file-preview-hint");
    hint.append(icon("preview"), element("span", "", "预览"));
    fileCell.append(hint);
    fileCell.addEventListener("click", () => openPreview(file));
  }
  nameCell.append(fileCell);
  const size = element("td", "size-column file-size", sizeText(file.allocated));
  size.title = `磁盘占用 ${sizeText(file.allocated)} · 文件大小 ${sizeText(file.size)}`;
  const modified = element("td", "date-column file-date", file.mtime ? date.format(file.mtime * 1000) : "—");
  const actions = element("td", "action-column");
  const actionGroup = element("div", "row-actions");
  const copy = iconButton("copy", `复制路径 · ${file.name}`);
  copy.title = `复制路径 · ${file.path}`;
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(file.path);
      notice("已复制文件路径。");
    } catch {
      notice(`无法自动复制路径。文件位置为 ${file.path}`, "warning");
    }
  });
  actionGroup.append(copy);
  if (file.trashable) {
    const trash = iconButton("trash", `移到废纸篓 · ${file.name}`, "trash");
    trash.addEventListener("click", () => openTrash([file]));
    actionGroup.append(trash);
  } else {
    const locked = element("span", "read-only");
    locked.title = file.lockedReason || "此文件仅供查看";
    locked.append(icon("lock"), element("span", "sr-only", locked.title));
    actionGroup.append(locked);
  }
  actions.append(actionGroup);
  row.append(checkCell, nameCell, size, modified, actions);
  return row;
}

function compareFiles(a, b) {
  const { field, direction } = sort;
  const difference = field === "name" ? collator.compare(a.name, b.name) : a[field] - b[field];
  const tie = field === "allocated" ? a.size - b.size : 0;
  return (difference || tie) * (direction === "ascending" ? 1 : -1) || collator.compare(a.path, b.path);
}

function renderFiles() {
  const query = byId("search").value.trim().toLocaleLowerCase();
  const minimum = Number(byId("minimum-size").value);
  const files = (snapshot?.files || []).filter((file) => {
    if ((rootId && file.rootId !== rootId) || (projectKey && file.projectKey !== projectKey)
      || Math.max(file.allocated, file.size) < minimum) return false;
    if (!query) return true;
    const source = rootsById.get(file.rootId);
    const project = source?.projects.find((item) => item.key === file.projectKey);
    return [file.path, source?.label, project?.label, project?.parentLabel]
      .some((text) => text?.toLocaleLowerCase().includes(query));
  }).sort(compareFiles);
  const pages = Math.max(1, Math.ceil(files.length / pageSize));
  page = Math.min(page, pages - 1);
  const start = page * pageSize;
  pageFiles = files.slice(start, start + pageSize);
  byId("file-rows").replaceChildren(...pageFiles.map(renderRow));
  byId("empty-state").hidden = files.length > 0;
  const hasFilter = Boolean(query || minimum);
  const filteredOut = snapshot && hasFilter;
  const unavailable = rootsById.get(rootId)?.error;
  byId("empty-symbol").setAttribute("href", scanning ? "#i-refresh" : unavailable || scanFailed ? "#i-alert" : "#i-folder");
  byId("empty-title").textContent = scanning ? "正在扫描文件" : !snapshot ? "还没有扫描结果" : unavailable ? "此目录无法扫描" : filteredOut ? "没有匹配的文件" : "清单中暂无大文件";
  byId("empty-copy").textContent = scanning ? "扫描时间取决于目录中的文件数量。" : !snapshot ? "请点击重新扫描。" : unavailable ? String(unavailable) : filteredOut ? "试试其他关键词或文件大小。" : "扫描结果会列出达到大小门槛的文件。";
  byId("visible-result").textContent = snapshot ? `${integer.format(files.length)} 个结果` : "";
  byId("page-summary").textContent = files.length ? `显示 ${integer.format(start + 1)}–${integer.format(start + pageFiles.length)} / ${integer.format(files.length)} 项` : "0 项";
  byId("page-count").textContent = `${page + 1} / ${pages}`;
  byId("page-prev").disabled = page === 0;
  byId("page-next").disabled = page + 1 >= pages;
  updateSelection();
}

function updateSelection() {
  const blocked = scanning || mutating || scanFailed;
  const selectable = pageFiles.filter((file) => file.trashable);
  const checkedCount = selectable.filter((file) => selected.has(file.id)).length;
  const checkPage = byId("check-page");
  checkPage.checked = selectable.length > 0 && checkedCount === selectable.length;
  checkPage.indeterminate = checkedCount > 0 && checkedCount < selectable.length;
  checkPage.disabled = blocked || !selectable.length || (selected.size >= selectionLimit && checkedCount === 0);
  for (const row of byId("file-rows").rows) {
    const file = filesById.get(row.dataset.fileId);
    if (!file) continue;
    const checked = selected.has(file.id);
    const checkbox = row.querySelector("input");
    checkbox.checked = checked;
    checkbox.disabled = blocked || !file.trashable || (!checked && selected.size >= selectionLimit);
    row.classList.toggle("is-selected", checked);
    const trash = row.querySelector(".trash");
    if (trash) trash.disabled = blocked;
  }
  byId("bulk-trash").hidden = !selected.size;
  byId("bulk-trash").disabled = blocked;
  byId("selected-count").textContent = integer.format(selected.size);
  byId("bulk-trash").title = selected.size >= selectionLimit ? "一次最多移动 200 个文件" : "";
}

function resetFilters() {
  selected.clear();
  page = 0;
  renderFiles();
}

function setSort(field) {
  const initialDirection = field === "name" ? "ascending" : "descending";
  sort = { field, direction: sort.field === field ? (sort.direction === "ascending" ? "descending" : "ascending") : initialDirection };
  for (const button of document.querySelectorAll("[data-sort]")) {
    const active = button.dataset.sort === field;
    const header = button.closest("th");
    if (active) header.setAttribute("aria-sort", sort.direction);
    else header.removeAttribute("aria-sort");
    button.querySelector("use").setAttribute("href", active ? (sort.direction === "ascending" ? "#i-up" : "#i-down") : "#i-sort");
  }
  page = 0;
  renderFiles();
}

function renderRootList() {
  const list = document.createDocumentFragment();
  for (const root of rootsById.values()) {
    const entry = element("div", "root-entry");
    const folder = icon("folder");
    const label = element("div");
    const rootPath = element("small", "", root.path);
    rootPath.title = root.path;
    label.append(element("strong", "", root.label), rootPath);
    if (root.error || root.skipped) {
      label.append(element("small", "root-issue", root.error || `有 ${integer.format(root.skipped)} 项无法读取。`));
    }
    entry.append(folder, label);
    if (root.custom) {
      const remove = element("button", "button remove-root", "移除目录");
      remove.type = "button";
      remove.disabled = mutating || scanning;
      remove.setAttribute("aria-label", `从扫描范围移除 ${root.label}`);
      remove.addEventListener("click", () => removeRoot(root));
      entry.append(remove);
    } else {
      entry.append(element("span", "root-badge", root.trashable ? "默认目录" : "默认 · 只读"));
    }
    list.append(entry);
  }
  byId("root-list").replaceChildren(list);
}

function openRoots() {
  feedback("roots-feedback");
  byId("roots-dialog").showModal();
  byId("root-path").focus();
}

async function updateRoots(path, method, payload, message) {
  if (mutating || scanning) return;
  mutating = true;
  setControls();
  feedback("roots-feedback", "正在更新扫描目录…");
  try {
    await request(path, method, payload);
    if (method === "POST") byId("root-path").value = "";
    const updated = await scan();
    if (updated) notice();
    feedback("roots-feedback", updated ? message : `${message} 刷新扫描结果失败，请重新扫描。`, !updated);
  } catch (error) {
    feedback("roots-feedback", error.message, true);
  } finally {
    mutating = false;
    setControls();
  }
}

function removeRoot(root) {
  return updateRoots("/api/roots/" + encodeURIComponent(root.id), "DELETE", undefined, `已从扫描范围移除「${root.label}」。`);
}

function openTrash(files) {
  if (scanning || mutating || scanFailed || !files.length) return;
  trashFiles = files;
  const total = files.reduce((sum, file) => sum + file.allocated, 0);
  byId("trash-description").textContent = `将 ${integer.format(files.length)} 个文件移到废纸篓，当前占用 ${sizeText(total)}。`;
  const preview = files.map((file) => {
    const item = element("li");
    const label = element("span", "trash-file-label");
    label.title = file.path;
    const source = rootsById.get(file.rootId)?.label || "本地";
    label.append(
      element("strong", "", file.name),
      element("small", "", `${source} / ${file.relativePath || file.name}`),
    );
    item.append(label, element("span", "", sizeText(file.allocated)));
    return item;
  });
  byId("trash-list").replaceChildren(...preview);
  feedback("trash-feedback");
  byId("trash-submit").lastElementChild.textContent = "确认移动";
  byId("trash-dialog").showModal();
  byId("trash-list").scrollTop = 0;
}

async function submitTrash() {
  if (mutating || !trashFiles.length) return;
  const files = trashFiles;
  const ids = files.map((file) => file.id);
  mutating = true;
  setControls();
  feedback("trash-feedback", "正在移到废纸篓…");
  byId("trash-submit").lastElementChild.textContent = "正在移动";
  try {
    const response = await request("/api/trash", "POST", { ids });
    const results = new Map((response.results || []).map((result) => [result.id, result]));
    const failed = files.filter((file) => !results.get(file.id)?.ok);
    const moved = files.length - failed.length;
    const details = failed.slice(0, 3).map((file) => `${file.name}（${results.get(file.id)?.error || "状态未知"}）`).join("、");
    const outcome = `已移到废纸篓 ${integer.format(moved)} 个文件。`
      + (failed.length ? `${integer.format(failed.length)} 个未能确认移动。${details}${failed.length > 3 ? " 等" : ""}` : "");
    byId("trash-dialog").close();
    const updated = await scan();
    notice(outcome + (updated ? "" : " 刷新扫描结果失败，请重新扫描。"), failed.length || !updated ? "warning" : "");
  } catch (error) {
    byId("trash-dialog").close();
    const updated = await scan();
    notice(`无法确认移动结果。${error.message}${updated ? " 已刷新文件清单。" : ""}`, "error");
  } finally {
    trashFiles = [];
    mutating = false;
    setControls();
  }
}

byId("all-roots").addEventListener("click", () => setRoot(""));
byId("clear-project").addEventListener("click", () => setRoot(projectKey ? rootId : ""));
byId("scan").addEventListener("click", async () => {
  notice();
  if (await scan()) {
    const issues = snapshot.roots.filter((root) => root.error || root.skipped).length;
    if (issues) notice(`扫描完成。有 ${integer.format(issues)} 个目录存在读取问题，可在「管理目录」查看详情。`, "warning");
  }
});
byId("search").addEventListener("input", resetFilters);
byId("minimum-size").addEventListener("change", resetFilters);
for (const button of document.querySelectorAll("[data-sort]")) button.addEventListener("click", () => setSort(button.dataset.sort));
byId("check-page").addEventListener("change", (event) => {
  for (const file of pageFiles) {
    if (!file.trashable) continue;
    if (!event.target.checked) selected.delete(file.id);
    else if (selected.size < selectionLimit) selected.add(file.id);
  }
  updateSelection();
});
byId("page-prev").addEventListener("click", () => {
  page -= 1;
  renderFiles();
});
byId("page-next").addEventListener("click", () => {
  page += 1;
  renderFiles();
});
byId("manage-roots").addEventListener("click", openRoots);
byId("manage-inline").addEventListener("click", openRoots);
byId("manage-mobile").addEventListener("click", openRoots);
byId("roots-close").addEventListener("click", () => byId("roots-dialog").close());
byId("add-root").addEventListener("submit", (event) => {
  event.preventDefault();
  const path = byId("root-path").value.trim();
  if (path) updateRoots("/api/roots", "POST", { path }, "已添加到扫描范围。");
});
byId("bulk-trash").addEventListener("click", () => openTrash([...selected].map((id) => filesById.get(id)).filter(Boolean)));
byId("trash-close").addEventListener("click", () => byId("trash-dialog").close());
byId("trash-cancel").addEventListener("click", () => byId("trash-dialog").close());
byId("trash-submit").addEventListener("click", submitTrash);
byId("preview-close").addEventListener("click", () => byId("preview-dialog").close());
byId("preview-dialog").addEventListener("close", (event) => {
  if (event.currentTarget.open) return;
  previewFile = null;
  clearPreviewMedia();
});
byId("preview-copy").addEventListener("click", async () => {
  const file = previewFile;
  if (!file) return;
  try {
    await navigator.clipboard.writeText(file.path);
    if (previewFile === file) feedback("preview-feedback", "已复制文件路径。");
  } catch {
    if (previewFile === file) feedback("preview-feedback", "无法自动复制，请选中上方路径手动复制。", true);
  }
});
byId("preview-open").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const file = previewFile;
  if (!file) return;
  button.disabled = true;
  feedback("preview-feedback", "正在打开…");
  try {
    await request("/api/open-preview", "POST", { id: file.id });
    if (previewFile === file) feedback("preview-feedback", "已交给系统应用打开。");
  } catch (error) {
    if (previewFile === file) feedback("preview-feedback", error.message, true);
  } finally {
    if (previewFile === file) button.disabled = false;
  }
});
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("cancel", (event) => {
    if (mutating) event.preventDefault();
  });
}

scan();
