// ==UserScript==
// @name         QQ Group Album Metadata Collector
// @namespace    local.qq-album-archiver
// @version      0.1.0
// @description  Capture QQ group album photo metadata already loaded by the page.
// @match        https://h5.qzone.qq.com/groupphoto/*
// @match        https://*.qq.com/*groupphoto*
// @run-at       document-start
// @grant        GM_download
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

(function () {
  "use strict";

  const records = new Map();
  const seenObjects = new WeakSet();
  let panel;
  let countLabel;
  let stateLabel;

  const cleanKey = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  const aliases = {
    photo_id: ["photoid", "picid", "pictureid", "lloc", "sloc", "id"],
    album_id: ["albumid", "topicid", "albumkey"],
    album_name: ["albumname", "albumtitle", "topicname", "album"],
    group_id: ["groupid", "groupuin", "qunid", "qunuin", "gc"],
    uploader: ["uploader", "uploadername", "nickname", "nick", "ownername"],
    uploader_id: ["uploaderid", "uin", "owneruin", "ownerid"],
    original_name: ["originalname", "filename", "name", "picname", "title"],
    upload_time: ["uploadtime", "uploadedat", "uploadtimestamp", "createtime", "addtime"],
    shoot_time: ["shoottime", "takentime", "takenat", "datetaken", "capturetime"],
  };
  const urlScore = {
    originurl: 10000, originalurl: 10000, rawurl: 9500, downloadurl: 9000,
    photourl: 8000, picurl: 7500, url: 5000, src: 3000,
    thumbnail: -5000, thumburl: -5000, smallurl: -4000,
  };

  function lookup(obj, name) {
    const wanted = new Set(aliases[name]);
    for (const [key, value] of Object.entries(obj)) {
      if (wanted.has(cleanKey(key)) && value !== null && typeof value !== "object") return value;
    }
    return "";
  }

  function normalizeUrl(value) {
    const text = String(value || "").replaceAll("\\/", "/").replaceAll("&amp;", "&");
    return text.startsWith("//") ? `https:${text}` : text;
  }

  function urlCandidates(value, depth = 0) {
    if (!value || depth > 4) return [];
    const result = [];
    if (Array.isArray(value)) {
      for (const item of value.slice(0, 100)) result.push(...urlCandidates(item, depth + 1));
      return result;
    }
    if (typeof value !== "object") return result;
    const width = Number(value.width || value.w || 0);
    const height = Number(value.height || value.h || 0);
    const sizeBonus = width && height ? Math.min(Math.floor((width * height) / 1000), 3000) : 0;
    for (const [key, child] of Object.entries(value)) {
      const normalized = cleanKey(key);
      if (typeof child === "string" && /^(https?:)?\/\//.test(child)) {
        result.push({ score: (urlScore[normalized] || 1000) + sizeBonus, url: normalizeUrl(child) });
      } else if (child && typeof child === "object") {
        const bonus = urlScore[normalized] || 0;
        result.push(...urlCandidates(child, depth + 1).map((item) => ({ ...item, score: item.score + bonus })));
      }
    }
    return result;
  }

  function photoFromObject(obj, source) {
    const candidates = urlCandidates(obj).sort((a, b) => b.score - a.score);
    const original_url = candidates[0]?.url || "";
    const photo_id = String(lookup(obj, "photo_id") || "");
    const original_name = String(lookup(obj, "original_name") || "");
    const upload_time = lookup(obj, "upload_time");
    const shoot_time = lookup(obj, "shoot_time");
    if (!original_url || !(photo_id || original_name || upload_time || shoot_time)) return null;
    return {
      photo_id,
      album_id: String(lookup(obj, "album_id") || ""),
      album_name: String(lookup(obj, "album_name") || document.title || "Unsorted"),
      group_id: String(lookup(obj, "group_id") || new URL(location.href).searchParams.get("groupId") || ""),
      uploader: String(lookup(obj, "uploader") || ""),
      uploader_id: String(lookup(obj, "uploader_id") || ""),
      original_name,
      original_url,
      upload_time,
      shoot_time,
      source_page: location.href,
      capture_source: source,
    };
  }

  function addRecord(record) {
    if (!record) return;
    const key = record.photo_id || record.original_url;
    const existing = records.get(key) || {};
    records.set(key, { ...existing, ...Object.fromEntries(Object.entries(record).filter(([, value]) => value !== "" && value !== null)) });
  }

  function scanPayload(payload, source = "page-json") {
    const stack = [payload];
    let scanned = 0;
    while (stack.length && scanned < 200000) {
      const current = stack.pop();
      scanned += 1;
      if (!current || typeof current !== "object") continue;
      if (seenObjects.has(current)) continue;
      seenObjects.add(current);
      if (Array.isArray(current)) {
        stack.push(...current);
      } else {
        addRecord(photoFromObject(current, source));
        stack.push(...Object.values(current));
      }
    }
    refreshPanel();
  }

  function parseAndScan(text, source) {
    if (typeof text !== "string" || text.length < 2) return;
    try {
      scanPayload(JSON.parse(text), source);
      return;
    } catch (_) {}
    const match = text.match(/^[\w$.]+\((.*)\)\s*;?$/s);
    if (match) {
      try { scanPayload(JSON.parse(match[1]), `${source}:jsonp`); } catch (_) {}
    }
  }

  const originalFetch = window.fetch;
  if (originalFetch) {
    window.fetch = async function (...args) {
      const response = await originalFetch.apply(this, args);
      response.clone().text().then((text) => parseAndScan(text, `fetch:${String(args[0])}`)).catch(() => {});
      return response;
    };
  }

  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__qqaUrl = url;
    this.addEventListener("load", function () {
      if (typeof this.responseText === "string") parseAndScan(this.responseText, `xhr:${url}`);
    });
    return originalOpen.call(this, method, url, ...rest);
  };

  const originalParse = JSON.parse;
  JSON.parse = function (...args) {
    const value = originalParse.apply(this, args);
    queueMicrotask(() => scanPayload(value, "json-parse"));
    return value;
  };

  function scanDom() {
    for (const image of document.images) {
      const url = image.dataset.original || image.dataset.src || image.currentSrc || image.src;
      if (!url || url.startsWith("data:")) continue;
      const container = image.closest("li, article, [class*=photo], [class*=item]") || image.parentElement;
      const text = container?.innerText || "";
      const date = text.match(/20\d{2}[年\-/]\d{1,2}[月\-/]\d{1,2}(?:日|\s)*(?:\d{1,2}:\d{2}(?::\d{2})?)?/)?.[0] || "";
      addRecord({
        photo_id: image.dataset.photoId || image.dataset.id || "",
        album_name: document.title || "Unsorted",
        original_name: image.alt || "",
        original_url: normalizeUrl(url),
        upload_time: date,
        source_page: location.href,
        capture_source: "dom-fallback",
      });
    }
    refreshPanel("已扫描当前页面");
  }

  function manifest() {
    return {
      schemaVersion: 1,
      exportedAt: new Date().toISOString(),
      source: location.href,
      photos: [...records.values()],
    };
  }

  function exportJson() {
    const blob = new Blob([JSON.stringify(manifest(), null, 2)], { type: "application/json;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `qq-group-album-${Date.now()}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }

  function sendLocal() {
    stateLabel.textContent = "正在发送";
    GM_xmlhttpRequest({
      method: "POST",
      url: "http://127.0.0.1:8765/import",
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify(manifest()),
      onload: (response) => {
        stateLabel.textContent = response.status === 200 ? "已发送到本地" : `发送失败 ${response.status}`;
      },
      onerror: () => { stateLabel.textContent = "本地程序未启动"; },
    });
  }

  function safeName(value, fallback) {
    return String(value || fallback).replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").replace(/[. ]+$/, "").slice(0, 120);
  }

  async function downloadAll() {
    const items = [...records.values()];
    if (!items.length) return refreshPanel("没有可下载记录");
    if (!confirm(`下载已捕获的 ${items.length} 个文件？浏览器可能要求允许多文件下载。`)) return;
    let next = 0;
    let finished = 0;
    async function worker() {
      while (next < items.length) {
        const item = items[next++];
        const urlPath = new URL(item.original_url, location.href).pathname;
        const suffix = urlPath.match(/\.[a-zA-Z0-9]{2,5}$/)?.[0] || ".jpg";
        const name = safeName(item.original_name, item.photo_id || `photo-${next}`);
        const filename = name.includes(".") ? name : `${name}${suffix}`;
        await new Promise((resolve) => {
          GM_download({
            url: item.original_url,
            name: `${safeName(item.album_name, "Unsorted")}/${filename}`,
            saveAs: false,
            onload: resolve,
            onerror: resolve,
            ontimeout: resolve,
          });
        });
        finished += 1;
        refreshPanel(`下载 ${finished}/${items.length}`);
        await new Promise((resolve) => setTimeout(resolve, 350));
      }
    }
    await Promise.all([worker(), worker(), worker()]);
    refreshPanel(`下载请求完成 ${finished}/${items.length}`);
  }

  function button(label, handler) {
    const element = document.createElement("button");
    element.textContent = label;
    Object.assign(element.style, {
      border: "1px solid #c8ccd0", background: "#fff", color: "#202124", padding: "6px 9px",
      borderRadius: "4px", cursor: "pointer", font: "12px Microsoft YaHei UI, sans-serif",
    });
    element.addEventListener("click", handler);
    return element;
  }

  function refreshPanel(message = "") {
    if (!panel) return;
    countLabel.textContent = `${records.size} 张`;
    if (message) stateLabel.textContent = message;
  }

  function buildPanel() {
    panel = document.createElement("div");
    Object.assign(panel.style, {
      position: "fixed", right: "16px", bottom: "16px", zIndex: "2147483647", width: "270px",
      background: "#f7f8fa", color: "#202124", border: "1px solid #b8bec6", borderRadius: "6px",
      boxShadow: "0 5px 20px rgba(0,0,0,.18)", padding: "12px", font: "13px Microsoft YaHei UI, sans-serif",
    });
    const top = document.createElement("div");
    top.style.display = "flex";
    top.style.justifyContent = "space-between";
    const title = document.createElement("strong");
    title.textContent = "QQ 相册归档采集器";
    countLabel = document.createElement("span");
    countLabel.textContent = "0 张";
    top.append(title, countLabel);
    const actions = document.createElement("div");
    Object.assign(actions.style, { display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "10px" });
    actions.append(
      button("扫描页面", scanDom),
      button("发送本地", sendLocal),
      button("导出 JSON", exportJson),
      button("下载原图", downloadAll),
    );
    stateLabel = document.createElement("div");
    Object.assign(stateLabel.style, { marginTop: "9px", color: "#5f6368", fontSize: "12px" });
    stateLabel.textContent = "正在监听页面数据";
    panel.append(top, actions, stateLabel);
    document.documentElement.appendChild(panel);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildPanel, { once: true });
  } else {
    buildPanel();
  }
})();
