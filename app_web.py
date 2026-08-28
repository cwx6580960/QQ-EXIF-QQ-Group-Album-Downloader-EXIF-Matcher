from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import FIRST_COMPLETED, CancelledError, ThreadPoolExecutor, wait
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from poc_auth.auth_poc import cookie_summary, local_qq_login, qr_login
from qq_album_archiver import (
    ArchiveDatabase,
    DownloadCancelled,
    FILE_ANALYSIS_VERSION,
    PhotoRecord,
    analyze_raw_file,
    apply_date_preview,
    create_library_copy,
    detect_media_suffix,
    find_exiftool,
    apply_manual_review,
    raw_destination_for,
    media_file_is_complete,
    MEDIA_EXTENSIONS,
    VIDEO_EXTENSIONS,
    write_reports,
)
from qq_group_api import AlbumRecord, QQGroupAlbumAPI, normalize_group_photo


if getattr(sys, "frozen", False):
    _APP_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "QQAlbumArchiver"
else:
    _APP_DATA_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = _APP_DATA_ROOT / "work" / "qq-album-runtime"
DEFAULT_ARCHIVE = Path.home() / "Pictures" / "QQGroupAlbumArchive"
DEFAULT_DOWNLOAD_WORKERS = 16
MAX_DOWNLOAD_WORKERS = 32
RESET_CONFIRMATION = "清空QQ归档"
HTTP_SERVER: ThreadingHTTPServer | None = None
SUMMARY_RECORD_FIELDS = (
    "record_key", "photo_id", "album_name", "original_name", "media_kind", "chosen_time", "date_source",
    "date_precision", "date_confidence", "date_review", "date_conflict", "date_note", "date_anchor_ids",
    "batch_key", "raw_path", "quality_class", "quality_width", "quality_height", "camera_model",
    "camera_model_source", "status",
)


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QQ 群相册归档器</title>
<style>
:root{color-scheme:light;--bg:#f5f6f8;--panel:#fff;--line:#d9dde3;--text:#202124;--muted:#68717c;--dark:#1f2937;--green:#166534;--amber:#9a6700;--red:#b42318}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px "Microsoft YaHei UI","Segoe UI",sans-serif;letter-spacing:0}
header{height:64px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center}.bar{width:min(1260px,100%);margin:auto;padding:0 22px;display:flex;align-items:center;justify-content:space-between}h1{font-size:20px;margin:0}header span{color:var(--muted);font-size:13px}
main{width:min(1260px,100%);margin:auto;padding:22px;display:grid;grid-template-columns:310px minmax(0,1fr);gap:24px}.sidebar{border-right:1px solid var(--line);padding-right:24px;min-height:calc(100vh - 110px)}section{margin-bottom:28px}h2{font-size:14px;margin:0 0 11px}.actions{display:flex;gap:8px;flex-wrap:wrap}.analysis-actions{width:100%;margin:10px 0;flex-wrap:nowrap;overflow-x:auto;padding-bottom:2px}.analysis-actions button{flex:0 0 auto;white-space:nowrap}
button{min-height:36px;border:1px solid #b8bec6;border-radius:5px;background:#fff;color:var(--text);padding:7px 12px;cursor:pointer;font:inherit}button:hover{background:#f1f3f5}button:disabled{color:#9aa0a6;background:#f4f5f6;cursor:default}.primary{background:var(--dark);border-color:var(--dark);color:#fff}.primary:hover{background:#111827}.danger{color:#b42318;border-color:#e0a6a0}.danger:hover{background:#fff1f0}
input,select{width:100%;min-height:36px;border:1px solid #b8bec6;border-radius:5px;background:#fff;padding:7px 10px;font:inherit}.row{display:grid;grid-template-columns:1fr auto;gap:8px}.number-setting{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:10px;color:var(--muted);font-size:12px}.number-setting select{width:96px;flex:0 0 96px}.qr{aspect-ratio:1;width:100%;max-width:280px;background:#fff;border:1px solid var(--line);display:grid;place-items:center;margin:12px 0}.qr img{width:88%;height:88%;image-rendering:pixelated}.placeholder{color:var(--muted);text-align:center;line-height:1.8;padding:20px}
.status{padding:12px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.status strong{display:block;margin-bottom:4px}.status span{color:var(--muted);overflow-wrap:anywhere}.ok{color:var(--green)}.waiting{color:var(--amber)}.error{color:var(--red)}
.toolbar{display:flex;gap:8px;align-items:center;justify-content:space-between;margin-bottom:10px}.summary{color:var(--muted);line-height:1.7}.filters{display:grid;grid-template-columns:180px 160px minmax(180px,1fr) auto;gap:8px;margin:10px 0}.table-options{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:10px 0}.table-options label{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px}.table-options select{width:auto;min-width:130px}.pagination{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.pagination button{min-height:32px;padding:5px 10px}.pagination span{color:var(--muted);min-width:220px;text-align:center}.table-wrap{width:100%;max-width:100%;background:#fff;border:1px solid var(--line);overflow:auto;max-height:520px}table{width:100%;border-collapse:collapse;table-layout:fixed}#albums table{min-width:720px}#photos table{min-width:1740px}#photos table.summary-table{table-layout:auto}#photos table.summary-table th,#photos table.summary-table td{white-space:nowrap;overflow-wrap:normal;word-break:normal}#photos table.summary-table td{padding-top:7px;padding-bottom:7px}#photos table.summary-table .album-col{min-width:190px}#photos table.summary-table .photo-col{min-width:280px}#photos table.summary-table .date{min-width:130px}#photos table.summary-table .source{min-width:220px}#photos table.summary-table .confidence{min-width:100px}#photos table.summary-table .batch-col{width:320px;max-width:320px}#photos table.summary-table .quality-col{min-width:280px}#photos table.summary-table .state{min-width:220px}.batch-text{display:block;width:300px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}#photos table.full-table{min-width:3600px;table-layout:auto}#photos table.full-table th,#photos table.full-table td{min-width:125px}#photos table.full-table th.wide,#photos table.full-table td.wide{min-width:260px}th,td{padding:9px 10px;border-bottom:1px solid #e7e9ed;text-align:left;vertical-align:top;white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere;word-break:break-word;line-height:1.45}th{position:sticky;top:0;background:#f3f5f7;font-size:12px;color:#555f6b;z-index:1;white-space:nowrap}#photos tbody tr{cursor:pointer}tbody tr:hover{background:#fafbfc}tbody tr.selected{background:#eef5ff}.check{width:44px}.count{width:90px}.album-col{width:190px}.photo-col{width:280px}.date{width:130px}.source{width:220px}.confidence{width:100px}.batch-col{width:300px}.quality-col{width:280px}.state{width:220px}.record-detail{margin-top:12px;background:#fff;border:1px solid var(--line);padding:14px;min-height:80px}.record-detail h3{margin:0 0 10px;font-size:14px}.detail-grid{display:grid;grid-template-columns:150px minmax(0,1fr);gap:7px 12px}.detail-grid dt{color:var(--muted);font-size:12px}.detail-grid dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}.record-detail pre{margin:8px 0 0;max-height:260px;overflow:auto;background:#f6f7f9;border:1px solid #e7e9ed;padding:10px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.45 Consolas,"Cascadia Mono",monospace}
.empty{padding:36px;text-align:center;color:var(--muted)}.output{display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:10px}.notice{font-size:12px;color:var(--muted);line-height:1.7;margin-top:10px}.progress{height:6px;background:#e7e9ed;margin-top:12px;overflow:hidden}.progress i{display:block;height:100%;width:0;background:#2563eb;transition:width .25s}.footer-status{margin-top:9px;color:var(--muted)}
@media(max-width:820px){header span{display:none}main{grid-template-columns:minmax(0,1fr);padding:16px}.sidebar,.content,section{min-width:0}.sidebar{border-right:0;border-bottom:1px solid var(--line);padding:0 0 20px;min-height:0}.qr{max-width:260px}.toolbar{align-items:flex-start;flex-direction:column}.filters{grid-template-columns:1fr 1fr}.filters input{grid-column:1/-1}.table-wrap{max-height:460px}.record-detail{padding:12px}.detail-grid{grid-template-columns:110px minmax(0,1fr)}.row{grid-template-columns:minmax(0,1fr) auto}input{min-width:0}}
</style><style>.review-panel{margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}.review-panel h4{margin:0 0 8px;font-size:13px}.review-form{display:grid;grid-template-columns:180px minmax(180px,1fr);gap:8px;align-items:center}.review-form input{min-width:0}.review-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}.review-actions button{min-height:32px;padding:5px 10px}.detail-collapse{border:0}.detail-collapse>summary{cursor:pointer;color:var(--dark);font-weight:600;padding:2px 0 10px}.detail-collapse .detail-body{padding-top:4px}</style></head><body>
<header><div class="bar"><h1>QQ 群相册归档器</h1><div class="actions"><span>原图保留 · 日期来源可追溯 · 本地运行</span><button id="shutdownBtn" title="关闭本地后台服务">关闭后台</button></div></div></header>
<main>
<aside class="sidebar">
 <section><h2>1. QQ 登录</h2><div class="actions"><button id="qrBtn" class="primary">二维码登录</button><button id="qqBtn">QQNT 快速登录</button></div><div class="qr" id="qr"><div class="placeholder">二维码会显示在这里<br>过期后自动刷新</div></div><div class="status"><strong id="loginStatus">等待登录</strong><span id="loginDetail">不读取密码，不上传 Cookie</span></div></section>
 <section><h2>2. QQ 群</h2><div class="row"><input id="group" inputmode="numeric" placeholder="输入 QQ 群号"><button id="loadAlbums">读取相册</button></div></section>
 <section><h2>3. 工作区与初始化</h2><input id="output" value="__DEFAULT_ARCHIVE__"><div class="actions"><button id="initBtn">初始化</button><button id="resetBtn" class="danger">彻底清空本地数据</button></div><label class="number-setting"><span>下载线程数</span><select id="downloadWorkers"><option value="1">1</option><option value="2">2</option><option value="4">4</option><option value="8">8</option><option value="16" selected>16</option><option value="24">24</option><option value="32">32</option></select></label><label class="notice"><input id="embed" type="checkbox" checked style="width:auto;min-height:0;margin-right:6px">补全整理副本 EXIF（日期、相机、GPS）</label><div class="notice" id="metadataMode">正在检测 ExifTool</div><div class="notice">初始化、扫描和下载分析是主流程；生成 <code>01_library</code> 整理副本属于可选步骤。原始文件不会被修改。</div><div class="notice">清空操作会删除本地数据库、运行时缓存，以及当前归档目录中的 <code>00_raw</code>、<code>01_library</code>、<code>metadata</code> 和 <code>reports</code>。</div></section>
</aside>
<div class="content">
 <section><div class="toolbar"><div><h2>3. 选择相册</h2><div class="summary" id="albumSummary">登录并读取群相册后显示</div></div><div class="actions"><button id="selectAll">全选</button><button id="scanBtn" class="primary">扫描照片元数据</button></div></div><div class="table-wrap" id="albums"><div class="empty">尚未读取相册</div></div></section>
 <section><div class="toolbar"><div><h2>照片日期分析</h2><div class="summary" id="photoSummary">尚未扫描</div><div class="summary" id="stageSummary">下载原图后才会生成最终日期结果</div><div class="summary" id="previewSummary">当前显示下载前初筛</div></div></div><div class="actions analysis-actions"><button id="refreshPreview">重新计算日期</button><button id="cancelBtn">停止任务</button><button id="downloadAnalyzeBtn" class="primary">1. 下载并分析原图</button><button id="libraryBtn">2. 生成整理副本</button></div><div class="filters"><select id="sourceFilter" aria-label="日期来源"><option value="">全部日期来源</option><option value="FILE_EXIF">文件原始 EXIF</option><option value="QQ_MAP_EXIF">QQ 服务端 EXIF</option><option value="FILE_SECONDARY">文件次级日期</option><option value="BATCH_NEIGHBOR">前后锚点推断</option><option value="BATCH_SINGLE_DATE">单一日期锚点</option><option value="QQ_UPLOAD_TIME">QQ 上传日期</option><option value="UNKNOWN">未知</option></select><select id="reviewFilter" aria-label="审核状态"><option value="">全部审核状态</option><option value="ready">可采用</option><option value="review">待复核</option><option value="fallback">上传日期回退</option></select><input id="photoSearch" placeholder="搜索相册、照片 ID、上传者或设备"><button id="downloadPreview">导出 CSV</button></div><div class="table-options"><label>字段视图<select id="viewMode" aria-label="字段视图"><option value="summary">摘要字段</option><option value="full">完整字段</option></select></label><div class="pagination"><button id="prevPage" title="上一页">上一页</button><span id="pageStatus">第 0 页</span><button id="nextPage" title="下一页">下一页</button><label>每页<select id="pageSize" aria-label="每页数量"><option value="100">100</option><option value="300">300</option><option value="1000">1000</option><option value="3000">3000</option></select></label></div></div><div class="table-wrap" id="photos"><div class="empty">下载并分析后将显示文件 EXIF、日期锚点和 CDN 质量</div></div><div class="record-detail" id="recordDetail">点击表格中的照片行查看完整字段</div><div class="progress"><i id="bar"></i></div><div class="footer-status" id="job">就绪</div></section>
</div></main>
<script>
const $=id=>document.getElementById(id);let albums=[];let lastQr="";let revision=0;let page=0;let pageSize=100;let viewMode="summary";
async function post(path,body={}){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});return r.json()}
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function detailValue(v){return v===null||v===undefined||v===''?'—':String(v)}
const summaryColumns=[
 {key:'album_name',label:'相册',className:'album-col'},
 {key:'photo_label',label:'照片 ID / 文件名',className:'photo-col'},
 {key:'chosen_date',label:'建议日期',className:'date'},
 {key:'date_summary',label:'来源 / 精度',className:'source'},
 {key:'date_confidence',label:'可信度',className:'confidence'},
 {key:'batch_summary',label:'批次 / 锚点',className:'batch-col'},
 {key:'quality_summary',label:'CDN 质量',className:'quality-col'},
 {key:'state_summary',label:'阶段 / 审核',className:'state'}
];
const fullColumns=[
 {key:'record_key',label:'记录键'}, {key:'photo_id',label:'照片 ID'}, {key:'album_id',label:'相册 ID'},
 {key:'album_name',label:'相册'}, {key:'group_id',label:'QQ群号'}, {key:'uploader',label:'上传者'}, {key:'uploader_id',label:'上传者 ID'},
 {key:'original_name',label:'原始文件名'}, {key:'original_url',label:'CDN 地址',className:'wide'}, {key:'media_kind',label:'媒体类型'}, {key:'media_mime',label:'媒体 MIME'}, {key:'upload_time',label:'QQ 上传时间'},
 {key:'shoot_time',label:'接口拍摄时间'}, {key:'source_page',label:'来源页面',className:'wide'}, {key:'capture_source',label:'采集来源'},
 {key:'raw_path',label:'00_raw 路径',className:'wide'}, {key:'local_path',label:'01_library 路径',className:'wide'},
 {key:'exif_time',label:'文件拍摄日期'}, {key:'file_secondary_time',label:'文件次级日期'}, {key:'file_exif_json',label:'文件 EXIF 摘要',className:'wide'},
 {key:'file_camera_make',label:'文件设备厂商'}, {key:'file_camera_model',label:'文件设备型号'}, {key:'file_lens_model',label:'文件镜头型号'}, {key:'file_gps_available',label:'文件 GPS'},
 {key:'qq_exif_time',label:'QQ 服务端 EXIF 日期'}, {key:'qq_exif_json',label:'QQ EXIF JSON',className:'wide'}, {key:'qq_camera_make',label:'QQ 设备厂商'}, {key:'qq_camera_model',label:'QQ 设备型号'}, {key:'qq_gps_available',label:'QQ GPS'},
 {key:'camera_make',label:'采用设备厂商'}, {key:'camera_make_source',label:'厂商来源'}, {key:'camera_model',label:'采用设备型号'}, {key:'camera_model_source',label:'型号来源'},
 {key:'gps_available',label:'采用 GPS'}, {key:'gps_source',label:'GPS 来源'}, {key:'chosen_time',label:'最终建议日期'}, {key:'date_source',label:'日期来源'},
 {key:'date_precision',label:'日期精度'}, {key:'date_confidence',label:'可信度'}, {key:'date_review',label:'审核状态'},
 {key:'date_conflict',label:'日期冲突'}, {key:'date_note',label:'日期说明',className:'wide'}, {key:'date_anchor_ids',label:'日期锚点'}, {key:'manual_reviewed',label:'人工复核'}, {key:'reviewed_at',label:'复核时间'}, {key:'reviewed_from_source',label:'复核前来源'}, {key:'review_note',label:'复核说明',className:'wide'},
 {key:'batch_key',label:'批次'}, {key:'quality_class',label:'CDN 质量'}, {key:'quality_width',label:'宽度'}, {key:'quality_height',label:'高度'},
 {key:'sha256',label:'SHA-256',className:'wide'}, {key:'status',label:'阶段状态'}, {key:'error',label:'错误信息',className:'wide'}
];
function columnValue(x,column){
 if(column.key==='photo_label') return x.original_name||x.photo_id;
 if(column.key==='chosen_date') return x.chosen_time?String(x.chosen_time).slice(0,10):'未知';
 if(column.key==='date_summary') return (x.date_source||'UNKNOWN')+' / '+(x.date_precision||'-');
 if(column.key==='batch_summary') return (x.batch_key||'-')+(x.date_anchor_ids?' ['+x.date_anchor_ids+']':'')+'\n'+(x.date_note||'');
 if(column.key==='quality_summary') return x.raw_path?((x.quality_class||'MAX_AVAILABLE')+(x.quality_width?' · '+x.quality_width+'×'+(x.quality_height||''): '')+(x.camera_model?' · '+x.camera_model+' ['+(x.camera_model_source||'未知来源')+']':'')):'待下载分析';
 if(column.key==='state_summary') return (x.status||'pending')+' · '+(x.date_review||'review')+(x.date_conflict?' · '+x.date_conflict:'');
 return detailValue(x[column.key]);
}
function renderPhotoTable(rows){
 if(!rows.length) return '<div class="empty">没有符合筛选条件的照片</div>';
 const columns=viewMode==='full'?fullColumns:summaryColumns;
 const className=viewMode==='full'?'full-table':'summary-table';
 return '<table class="'+className+'"><thead><tr>'+columns.map(c=>'<th class="'+(c.className||'')+'">'+esc(c.label)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(x=>'<tr data-key="'+esc(x.record_key)+'" onclick="showRecord(\''+esc(x.record_key)+'\',this)">'+columns.map(c=>{const value=columnValue(x,c);const content=c.key==='batch_summary'?'<span class="batch-text" title="'+esc(value)+'">'+esc(value)+'</span>':esc(value);return '<td class="'+(c.className||'')+'">'+content+'</td>'}).join('')+'</tr>').join('')+'</tbody></table>';
}
async function showRecord(key,row){document.querySelectorAll('#photos tbody tr.selected').forEach(x=>x.classList.remove('selected'));if(row)row.classList.add('selected');const r=await fetch('/api/record?key='+encodeURIComponent(key),{cache:'no-store'});if(!r.ok){$('recordDetail').innerHTML='<div class="empty">无法读取该照片详情</div>';return}const x=await r.json();const labels={record_key:'记录键',photo_id:'照片 ID',album_id:'相册 ID',album_name:'相册',group_id:'QQ群号',uploader:'上传者',uploader_id:'上传者 ID',original_name:'原始文件名',original_url:'CDN 地址',upload_time:'QQ 上传时间',shoot_time:'接口拍摄时间',source_page:'来源页面',capture_source:'采集来源',raw_path:'00_raw 路径',local_path:'01_library 路径',exif_time:'文件 DateTimeOriginal',file_secondary_time:'文件次级日期',file_exif_json:'文件 EXIF 摘要',file_camera_make:'文件设备厂商',file_camera_model:'文件设备型号',file_lens_model:'文件镜头型号',file_gps_available:'文件 GPS',qq_exif_time:'QQ 服务端 EXIF 时间',qq_exif_json:'QQ EXIF JSON',qq_camera_make:'QQ 设备厂商',qq_camera_model:'QQ 设备型号',qq_gps_available:'QQ GPS',camera_make:'采用设备厂商',camera_make_source:'厂商来源',camera_model:'采用设备型号',camera_model_source:'型号来源',gps_available:'采用 GPS',gps_source:'GPS 来源',chosen_time:'最终建议日期',date_source:'日期来源',date_precision:'日期精度',date_confidence:'可信度',date_review:'审核状态',date_conflict:'冲突',date_note:'说明',date_anchor_ids:'日期锚点',batch_key:'批次',quality_class:'CDN 质量',quality_width:'文件宽度',quality_height:'文件高度',sha256:'SHA-256',analysis_version:'分析版本',status:'阶段状态',error:'错误'};const keys=Object.keys(labels);$('recordDetail').innerHTML='<h3>照片详情：'+esc(detailValue(x.original_name||x.photo_id))+'</h3><dl class="detail-grid">'+keys.map(k=>'<dt>'+labels[k]+'</dt><dd>'+esc(detailValue(x[k]))+'</dd>').join('')+'</dl><details><summary>原始接口 JSON</summary><pre>'+esc(detailValue(x.raw_json))+'</pre></details>'}
async function refresh(){try{const s=await fetch('/api/status',{cache:'no-store'}).then(r=>r.json());$('loginStatus').textContent=s.login_status;$('loginDetail').textContent=s.login_detail;$('loginStatus').className=s.login_level||'';$('qrBtn').disabled=s.auth_running;$('qqBtn').disabled=s.auth_running;$('downloadAnalyzeBtn').disabled=s.job_running;$('libraryBtn').disabled=s.job_running;$('refreshPreview').disabled=s.job_running;$('cancelBtn').disabled=!s.job_running;$('downloadWorkers').disabled=s.job_running;$('resetBtn').disabled=s.job_running||s.auth_running;$('shutdownBtn').disabled=s.job_running||s.auth_running;if(s.qr&&s.qr!==lastQr){lastQr=s.qr;$('qr').innerHTML=`<img src="${s.qr}" alt="QQ 登录二维码">`}const transfer=s.job_running&&s.download_workers?` · 活动传输 ${s.active_downloads}/${s.download_workers} · 峰值 ${s.peak_downloads} · ${Number(s.download_mib_s||0).toFixed(2)} MiB/s`:s.job_running&&s.library_workers?` · EXIF 活动线程 ${s.active_library}/${s.library_workers} · 峰值 ${s.peak_library}`:'';$('job').textContent=s.job_status+transfer;$('bar').style.width=`${s.progress||0}%`;$('metadataMode').textContent=s.exiftool_available?'已检测到 ExifTool：可按缺失字段补全日期、相机和 GPS':'未检测到 ExifTool：仅保留原文件与 Windows 文件时间，来源信息写入数据库';$('embed').disabled=!s.exiftool_available||s.job_running;if(!s.exiftool_available)$('embed').checked=false;if(s.records_changed!==revision){revision=s.records_changed;loadPhotos()} }catch(e){$('job').textContent='本地服务连接中断'}}
function renderAlbums(){if(!albums.length){$('albums').innerHTML='<div class="empty">没有可用相册</div>';return}$('albums').innerHTML=`<table><thead><tr><th class="check"><input id="allBox" type="checkbox"></th><th>相册名称</th><th class="count">照片数</th></tr></thead><tbody>${albums.map(a=>`<tr><td><input class="albumBox" type="checkbox" value="${esc(a.album_id)}" checked></td><td title="${esc(a.title)}">${esc(a.title)}</td><td>${a.photo_count}</td></tr>`).join('')}</tbody></table>`;$('allBox').checked=true;$('allBox').onchange=e=>document.querySelectorAll('.albumBox').forEach(x=>x.checked=e.target.checked)}
  function recordQuery(includePage=true){const p=new URLSearchParams();if(includePage){p.set('limit',String(pageSize));p.set('offset',String(page*pageSize));p.set('view',viewMode)}if($('sourceFilter').value)p.set('source',$('sourceFilter').value);if($('reviewFilter').value)p.set('review',$('reviewFilter').value);if($('photoSearch').value.trim())p.set('q',$('photoSearch').value.trim());return p.toString()}
async function loadPhotos(){const requestedPage=page;const r=await fetch('/api/records?'+recordQuery(),{cache:'no-store'}).then(r=>r.json());const total=Number(r.total||0);const pageCount=Math.max(1,Math.ceil(total/pageSize));if(total===0)page=0;else if(page>=pageCount){page=pageCount-1;if(page!==requestedPage)return loadPhotos()}const s=r.preview_summary||{},p=r.stage_summary||{};const src=Object.entries(s.sources||{}).map(([k,v])=>`${k} ${v}`).join(' · ');const review=Object.entries(s.reviews||{}).map(([k,v])=>`${k} ${v}`).join(' · ');$('photoSummary').textContent=`筛选结果 ${total} 条；有上传时间 ${r.with_upload_time} 条；全部批次 ${s.sessions||0}`;$('stageSummary').textContent=`原图：未下载 ${p.pending||0} · 已分析 ${p.analyzed||0} · 下载失败 ${p.download_failed||0}；整理副本 ${p.archived||0} · 整理失败 ${p.organize_failed||0}`;$('previewSummary').textContent=`${p.pending?'下载前初筛':'下载后最终预演'}；来源：${src||'待生成'}；审核：${review||'待生成'}；冲突 ${s.conflicts||0}`;$('pageStatus').textContent=total?`第 ${page+1} / ${pageCount} 页（${page*pageSize+1}-${Math.min((page+1)*pageSize,total)} / ${total}）`:'第 0 页（0 条）';$('prevPage').disabled=page<=0;$('nextPage').disabled=page>=pageCount-1||!total;$('photos').innerHTML=renderPhotoTable(r.rows||[])}
$('qrBtn').onclick=()=>post('/api/login/qr');$('qqBtn').onclick=()=>post('/api/login/local');
$('loadAlbums').onclick=async()=>{const group=$('group').value.trim();const r=await post('/api/albums',{group});if(!r.ok){$('albumSummary').textContent=r.message;albums=[]}else{albums=r.albums;$('albumSummary').textContent=`${albums.length} 个相册，共 ${albums.reduce((n,a)=>n+a.photo_count,0)} 张`;renderAlbums()}};
$('selectAll').onclick=()=>{const boxes=[...document.querySelectorAll('.albumBox')];const value=boxes.some(x=>!x.checked);boxes.forEach(x=>x.checked=value)};
 $('scanBtn').onclick=async()=>{const ids=[...document.querySelectorAll('.albumBox:checked')].map(x=>x.value);const r=await post('/api/scan',{album_ids:ids});$('job').textContent=r.message};
 $('refreshPreview').onclick=async()=>{const r=await post('/api/preview');$('job').textContent=r.message||'日期预演已刷新';await loadPhotos()};
 function resetPhotoPage(){page=0;loadPhotos()}
 $('sourceFilter').onchange=resetPhotoPage;$('reviewFilter').onchange=resetPhotoPage;let searchTimer;$('photoSearch').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(resetPhotoPage,250)};$('viewMode').onchange=e=>{viewMode=e.target.value;loadPhotos()};$('pageSize').onchange=e=>{pageSize=Number(e.target.value)||100;page=0;loadPhotos()};$('prevPage').onclick=()=>{if(page>0){page-=1;loadPhotos()}};$('nextPage').onclick=()=>{page+=1;loadPhotos()};$('downloadPreview').onclick=()=>{location.href='/api/date-preview.csv?'+recordQuery(false)};
$('downloadAnalyzeBtn').onclick=async()=>{const workers=Math.max(1,Math.min(32,Number($('downloadWorkers').value)||16));$('downloadWorkers').value=String(workers);const r=await post('/api/download-analyze',{output:$('output').value.trim(),workers});$('job').textContent=r.message};
$('libraryBtn').onclick=async()=>{const workers=Math.max(1,Math.min(32,Number($('downloadWorkers').value)||16));$('downloadWorkers').value=String(workers);const r=await post('/api/library',{output:$('output').value.trim(),embed:$('embed').checked,workers});$('job').textContent=r.message};
$('cancelBtn').onclick=async()=>{const r=await post('/api/cancel');$('job').textContent=r.message};
$('shutdownBtn').onclick=async()=>{if(!confirm('确认关闭本地后台服务？'))return;const r=await post('/api/shutdown');$('job').textContent=r.message||'后台服务正在关闭'};
const issueFilter=document.createElement('select');issueFilter.id='issueFilter';issueFilter.setAttribute('aria-label','问题筛选');issueFilter.innerHTML='<option value="">问题筛选</option><option value="review">待复核</option><option value="organize_failed">整理失败</option><option value="download_failed">下载失败</option><option value="pending">未下载或未分析</option>';document.querySelector('.filters').insertBefore(issueFilter,$('photoSearch'));const baseRecordQuery=recordQuery;recordQuery=function(includePage=true){const p=new URLSearchParams(baseRecordQuery(includePage));if(issueFilter.value)p.set('issue',issueFilter.value);return p.toString()};issueFilter.onchange=resetPhotoPage;
async function submitReview(key,action){const date=document.getElementById('reviewDate')?.value||'';const note=document.getElementById('reviewNote')?.value||'';const r=await post('/api/review',{key,action,chosen_date:date,note});$('job').textContent=r.message||'审核结果已保存';if(r.ok){await loadPhotos();await showRecord(key,document.querySelector('#photos tr[data-key="'+CSS.escape(key)+'"]'))}}
const originalShowRecord=showRecord;showRecord=async function(key,row){document.querySelectorAll('#photos tbody tr.selected').forEach(x=>x.classList.remove('selected'));if(row)row.classList.add('selected');const r=await fetch('/api/record?key='+encodeURIComponent(key),{cache:'no-store'});if(!r.ok){$('recordDetail').innerHTML='<div class="empty">无法读取该照片详情</div>';return}const x=await r.json();const labels={record_key:'记录键',photo_id:'照片 ID',album_id:'相册 ID',album_name:'相册',group_id:'QQ群号',uploader:'上传者',uploader_id:'上传者 ID',original_name:'原始文件名',original_url:'CDN 地址',upload_time:'QQ 上传时间',shoot_time:'接口拍摄时间',source_page:'来源页面',capture_source:'采集来源',raw_path:'00_raw 路径',local_path:'01_library 路径',exif_time:'文件 DateTimeOriginal',file_secondary_time:'文件次级日期',file_exif_json:'文件 EXIF 摘要',file_camera_make:'文件设备厂商',file_camera_model:'文件设备型号',file_lens_model:'文件镜头型号',file_gps_available:'文件 GPS',qq_exif_time:'QQ 服务端 EXIF 日期',qq_exif_json:'QQ EXIF JSON',qq_camera_make:'QQ 设备厂商',qq_camera_model:'QQ 设备型号',qq_gps_available:'QQ GPS',camera_make:'采用设备厂商',camera_make_source:'厂商来源',camera_model:'采用设备型号',camera_model_source:'型号来源',gps_available:'采用 GPS',gps_source:'GPS 来源',chosen_time:'最终建议日期',date_source:'日期来源',date_precision:'日期精度',date_confidence:'可信度',date_review:'审核状态',date_conflict:'冲突',date_note:'说明',date_anchor_ids:'日期锚点',manual_reviewed:'人工复核',reviewed_at:'复核时间',reviewed_from_source:'复核前来源',review_note:'复核说明',batch_key:'批次',quality_class:'CDN 质量',quality_width:'宽度',quality_height:'高度',sha256:'SHA-256',analysis_version:'分析版本',status:'阶段状态',error:'错误'};const keys=Object.keys(labels);const suggested=x.chosen_time?String(x.chosen_time).slice(0,10):'';const enabled=Boolean(x.raw_path||x.upload_time);const id=esc(x.record_key);$('recordDetail').innerHTML='<h3>照片详情：'+esc(detailValue(x.original_name||x.photo_id))+'</h3><dl class="detail-grid">'+keys.map(k=>'<dt>'+labels[k]+'</dt><dd>'+esc(detailValue(x[k]))+'</dd>').join('')+'</dl><div class="review-panel"><h4>日期复核</h4><div class="notice">当前建议：'+esc(suggested||'无')+'；来源：'+esc(x.date_source||'UNKNOWN')+'；'+esc(x.date_note||'请根据原始 EXIF、QQ 服务端 EXIF、批次锚点和上传日期判断')+'</div><div class="review-form"><label for="reviewDate">确认日期（仅到日）</label><input id="reviewDate" type="date" value="'+esc(suggested)+'" '+(enabled?'':'disabled')+'><label for="reviewNote">复核说明</label><input id="reviewNote" placeholder="可选，例如与同批照片一致" value="'+esc(x.review_note||'')+'" '+(enabled?'':'disabled')+'></div><div class="review-actions"><button class="primary" onclick="submitReview(\''+id+'\',\'accept\')" '+(enabled?'':'disabled')+'>接受当前建议</button><button onclick="submitReview(\''+id+'\',\'manual\')" '+(enabled?'':'disabled')+'>确认手动日期</button><button onclick="submitReview(\''+id+'\',\'keep\')">保留待复核</button></div></div><details><summary>原始接口 JSON</summary><pre>'+esc(detailValue(x.raw_json))+'</pre></details>'};
if(![...$('sourceFilter').options].some(x=>x.value==='MANUAL_REVIEW'))$('sourceFilter').insertAdjacentHTML('beforeend','<option value="MANUAL_REVIEW">人工复核</option>');
const bulkReviewBtn=document.createElement('button');bulkReviewBtn.id='bulkReviewBtn';bulkReviewBtn.textContent='一键接受当前建议';bulkReviewBtn.title='确认当前待复核列表中的所有建议日期';document.querySelector('.filters').insertBefore(bulkReviewBtn,$('photoSearch'));bulkReviewBtn.onclick=async()=>{const currentIssue=issueFilter.value;if(currentIssue!=='review'){issueFilter.value='review';resetPhotoPage()}const preview=await fetch('/api/records?issue=review&limit=1',{cache:'no-store'}).then(r=>r.json());const total=Number(preview.total||0);if(!total){$('job').textContent='当前没有待复核记录';return}if(!confirm('将一次性接受 '+total+' 条待复核记录的当前建议日期。此操作会把它们标记为人工复核通过，之后可生成整理副本。是否继续？'))return;bulkReviewBtn.disabled=true;const result=await post('/api/review-bulk',{action:'accept'});bulkReviewBtn.disabled=false;$('job').textContent=result.message||'批量复核完成';await loadPhotos()};
const mediaFilter=document.createElement('select');mediaFilter.id='mediaFilter';mediaFilter.setAttribute('aria-label','媒体类型');mediaFilter.innerHTML='<option value="">全部媒体类型</option><option value="image">图片</option><option value="video">视频</option>';document.querySelector('.filters').insertBefore(mediaFilter,$('photoSearch'));const queryWithMedia=recordQuery;recordQuery=function(includePage=true){const p=new URLSearchParams(queryWithMedia(includePage));if(mediaFilter.value)p.set('media',mediaFilter.value);return p.toString()};mediaFilter.onchange=resetPhotoPage;
document.querySelector('h1').textContent='QQ 群相册下载与 EXIF 匹配';document.querySelectorAll('h2')[3].textContent='4. 选择相册';document.querySelectorAll('h2')[4].textContent='下载与 EXIF 匹配';$('downloadAnalyzeBtn').textContent='下载并分析原图';$('libraryBtn').textContent='可选：生成整理副本';
$('initBtn').onclick=async()=>{if(!confirm('初始化只重建媒体、EXIF 和日期索引，不下载文件，也不生成整理副本。是否继续？'))return;const r=await post('/api/initialize',{output:$('output').value.trim()});$('job').textContent=r.message||'初始化已开始'};
$('resetBtn').onclick=async()=>{if(!confirm('这是破坏性操作：将清除本地数据库、运行时缓存，以及当前归档目录中的 00_raw、01_library、metadata 和 reports。程序代码不会删除。是否继续？'))return;const phrase=prompt('请输入确认词：清空QQ归档');if(phrase!=='清空QQ归档'){$('job').textContent='已取消清空：确认词不正确';return}const r=await post('/api/reset',{output:$('output').value.trim(),confirm:phrase});$('job').textContent=r.message||'本地数据清空完成';if(r.ok){albums=[];$('group').value='';$('albumSummary').textContent='本地数据已清空，请重新登录并读取相册';renderAlbums();$('recordDetail').textContent='点击表格中的照片行查看完整字段';page=0;revision=-1;await loadPhotos()}};
const baseRefresh=refresh;refresh=async function(){await baseRefresh();if($('initBtn'))$('initBtn').disabled=!$('cancelBtn').disabled};
const baseShowRecord=showRecord;showRecord=async function(key,row){await baseShowRecord(key,row);const panel=$('recordDetail');if(!panel||!panel.innerHTML||panel.querySelector('.detail-collapse'))return;const body=panel.innerHTML;const title=panel.querySelector('h3')?.textContent||'照片详情';panel.innerHTML='<details class="detail-collapse"><summary>'+esc(title)+'（点击展开）</summary><div class="detail-body">'+body+'</div></details>';const raw=panel.querySelector('.detail-body details:last-child');if(raw){raw.dataset.rawKey=key;const pre=raw.querySelector('pre');if(pre)pre.textContent='展开后加载原始接口 JSON'} };
document.addEventListener('toggle',async event=>{const box=event.target;if(!(box instanceof HTMLDetailsElement)||!box.open||!box.dataset.rawKey||box.dataset.loaded)return;box.dataset.loaded='1';const pre=box.querySelector('pre');if(!pre)return;try{const raw=await fetch('/api/record?key='+encodeURIComponent(box.dataset.rawKey)+'&raw=1',{cache:'no-store'}).then(r=>r.json());pre.textContent=detailValue(raw.raw_json)}catch(error){pre.textContent='原始接口 JSON 加载失败'}} ,true);
 setInterval(refresh,900);refresh();loadPhotos();
</script></body></html>'''.replace("__DEFAULT_ARCHIVE__", str(DEFAULT_ARCHIVE))


class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        self.session = None
        self.uin = ""
        self.api: QQGroupAlbumAPI | None = None
        self.group_id = ""
        self.albums: dict[str, AlbumRecord] = {}
        self.auth_running = False
        self.login_status = "等待登录"
        self.login_detail = "不读取密码，不上传 Cookie"
        self.login_level = ""
        self.qr = ""
        self.job_running = False
        self.job_status = "就绪"
        self.progress = 0
        self.records_changed = 0
        self._records_cache_revision = -1
        self._records_cache: list[PhotoRecord] = []
        self.preview_summary = {}
        self.cancel_event = threading.Event()
        self.download_workers = 0
        self.active_downloads = 0
        self.peak_downloads = 0
        self.downloaded_bytes = 0
        self.downloaded_files = 0
        self.download_started_clock = 0.0
        self.library_workers = 0
        self.active_library = 0
        self.peak_library = 0
        self.exiftool_available = bool(find_exiftool())
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        self.database = ArchiveDatabase(RUNTIME_DIR / "archive.sqlite3")
        self._repair_captured_metadata()
        self.rebuild_date_preview()

    def _repair_captured_metadata(self):
        repaired = []
        for record in self.database.all():
            if not record.raw_json:
                continue
            try:
                raw = json.loads(record.raw_json)
                album = AlbumRecord(record.album_id, record.album_name, 0, {})
                normalized = normalize_group_photo(raw, record.group_id, album)
                if normalized is None:
                    continue
                changed = False
                for name in (
                    "upload_time", "shoot_time", "uploader", "uploader_id", "original_name", "original_url",
                    "media_kind", "media_mime",
                    "qq_exif_time", "qq_exif_json", "qq_camera_make", "qq_camera_model", "qq_gps_available",
                    "quality_class",
                ):
                    value = getattr(normalized, name)
                    if value and getattr(record, name) != value:
                        setattr(record, name, value)
                        changed = True
                if record.raw_path and Path(record.raw_path).exists():
                    raw_file = Path(record.raw_path)
                    detected_suffix = detect_media_suffix(raw_file)
                    if record.media_kind == "video" and detected_suffix not in VIDEO_EXTENSIONS:
                        legacy_raw = raw_file.with_name(raw_file.stem + "__cover" + raw_file.suffix)
                        counter = 2
                        while legacy_raw.exists():
                            legacy_raw = raw_file.with_name(raw_file.stem + f"__cover_{counter}" + raw_file.suffix)
                            counter += 1
                        raw_file.replace(legacy_raw)
                        if record.local_path and Path(record.local_path).exists():
                            old_library = Path(record.local_path)
                            legacy_library = old_library.with_name(old_library.stem + "__cover" + old_library.suffix)
                            counter = 2
                            while legacy_library.exists():
                                legacy_library = old_library.with_name(old_library.stem + f"__cover_{counter}" + old_library.suffix)
                                counter += 1
                            old_library.replace(legacy_library)
                        record.raw_path = ""
                        record.local_path = ""
                        record.status = "download_failed"
                        record.error = "QQ 接口识别为视频，但现有文件是视频封面；需重新下载视频原文件"
                        changed = True
                    elif record.media_kind != "video" and detected_suffix in MEDIA_EXTENSIONS and raw_file.suffix.lower() != detected_suffix:
                        corrected = raw_file.with_suffix(detected_suffix)
                        if corrected.exists():
                            corrected = raw_file.with_name(raw_file.stem + "__" + detected_suffix.lstrip("." ) + detected_suffix)
                        raw_file.replace(corrected)
                        record.raw_path = str(corrected)
                        changed = True
                if changed:
                    repaired.append(record)
            except Exception:
                continue
        if repaired:
            self.database.replace(repaired)
            self.records_changed += 1

    def rebuild_date_preview(self):
        records = self.database.all()
        self.preview_summary = apply_date_preview(records)
        if records:
            self.database.replace(records)
        self.records_changed += 1
        return self.preview_summary

    def stage_summary(self, records=None):
        rows = records if records is not None else self.database.all()
        raw_ready = sum(bool(row.raw_path and Path(row.raw_path).exists()) for row in rows)
        archived = sum(bool(row.local_path and Path(row.local_path).exists()) for row in rows)
        return {
            "total": len(rows),
            "pending": len(rows) - raw_ready,
            "analyzed": raw_ready,
            "archived": archived,
            "download_failed": sum(row.status == "download_failed" for row in rows),
            "organize_failed": sum(row.status == "organize_failed" for row in rows),
        }

    def update(self, **values):
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def records(self, include_raw_json: bool = False) -> list[PhotoRecord]:
        """Reuse the metadata snapshot for fast table/filter requests."""
        with self.lock:
            if include_raw_json:
                return self.database.all(include_raw_json=True)
            if self._records_cache_revision != self.records_changed:
                self._records_cache = self.database.all(include_raw_json=False)
                self._records_cache_revision = self.records_changed
            return list(self._records_cache)

    def record_changed(self, record: PhotoRecord, previous: tuple[str, str, bool], job_status: str = "") -> None:
        """Update the fast table snapshot without rebuilding all date batches."""
        old_source, old_review, old_conflict = previous
        new_source = record.date_source or "UNKNOWN"
        new_review = record.date_review or "review"
        with self.lock:
            summary = dict(self.preview_summary or {})
            sources = dict(summary.get("sources") or {})
            reviews = dict(summary.get("reviews") or {})
            sources[old_source] = max(0, int(sources.get(old_source, 0)) - 1)
            sources[new_source] = int(sources.get(new_source, 0)) + 1
            reviews[old_review] = max(0, int(reviews.get(old_review, 0)) - 1)
            reviews[new_review] = int(reviews.get(new_review, 0)) + 1
            summary["sources"] = {key: value for key, value in sources.items() if value}
            summary["reviews"] = {key: value for key, value in reviews.items() if value}
            summary["conflicts"] = max(0, int(summary.get("conflicts", 0)) - int(old_conflict)) + int(bool(record.date_conflict))
            self.preview_summary = summary
            self.records_changed += 1
            if self._records_cache_revision == self.records_changed - 1:
                for index, cached in enumerate(self._records_cache):
                    if cached.record_key == record.record_key:
                        self._records_cache[index] = record
                        break
                self._records_cache_revision = self.records_changed
            if job_status:
                self.job_status = job_status

    def reset_download_metrics(self, workers: int):
        with self.lock:
            self.download_workers = workers
            self.active_downloads = 0
            self.peak_downloads = 0
            self.downloaded_bytes = 0
            self.downloaded_files = 0
            self.download_started_clock = 0.0

    def begin_download(self):
        with self.lock:
            if not self.download_started_clock:
                self.download_started_clock = time.monotonic()
            self.active_downloads += 1
            self.peak_downloads = max(self.peak_downloads, self.active_downloads)

    def finish_download(self, downloaded_bytes: int = 0):
        with self.lock:
            self.active_downloads = max(0, self.active_downloads - 1)
            if downloaded_bytes > 0:
                self.downloaded_bytes += downloaded_bytes
                self.downloaded_files += 1

    def snapshot(self):
        with self.lock:
            elapsed = time.monotonic() - self.download_started_clock if self.download_started_clock else 0.0
            return {
                "auth_running": self.auth_running,
                "login_status": self.login_status,
                "login_detail": self.login_detail,
                "login_level": self.login_level,
                "qr": self.qr,
                "job_status": self.job_status,
                "job_running": self.job_running,
                "progress": self.progress,
                "records_changed": self.records_changed,
                "exiftool_available": self.exiftool_available,
                "preview_summary": self.preview_summary,
                "download_workers": self.download_workers,
                "active_downloads": self.active_downloads,
                "peak_downloads": self.peak_downloads,
                "downloaded_files": self.downloaded_files,
                "download_mib_s": (self.downloaded_bytes / 1024 / 1024 / elapsed) if elapsed > 0 else 0.0,
                "library_workers": self.library_workers,
                "active_library": self.active_library,
                "peak_library": self.peak_library,
            }


STATE = AppState()


def _remove_local_path(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def reset_local_data(output: Path) -> dict[str, object]:
    """Remove app-owned local data while leaving code and unrelated output files intact."""
    output = output.expanduser().resolve()
    if not output.is_absolute() or output == Path(output.anchor) or output == Path.home():
        raise ValueError("归档目录过于宽泛，拒绝清空")
    if output.exists() and not output.is_dir():
        raise ValueError("归档路径不是目录")
    archive_targets = [output / name for name in ("00_raw", "01_library", "metadata", "reports")]
    with STATE.lock:
        if STATE.job_running:
            raise RuntimeError("请先停止当前任务")
        if STATE.auth_running:
            raise RuntimeError("请等待登录任务结束")
        STATE.database.close()
        for target in archive_targets:
            _remove_local_path(target)
        if RUNTIME_DIR.exists():
            for child in list(RUNTIME_DIR.iterdir()):
                _remove_local_path(child)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        STATE.database = ArchiveDatabase(RUNTIME_DIR / "archive.sqlite3")
        STATE.session = None
        STATE.api = None
        STATE.uin = ""
        STATE.group_id = ""
        STATE.albums = {}
        STATE.auth_running = False
        STATE.login_status = "等待登录"
        STATE.login_detail = "本地数据已清空，不读取密码，不上传 Cookie"
        STATE.login_level = ""
        STATE.qr = ""
        STATE.cancel_event.clear()
        STATE.job_running = False
        STATE.job_status = "本地数据已清空，可以重新登录并下载"
        STATE.progress = 0
        STATE.preview_summary = {}
        STATE._records_cache = []
        STATE._records_cache_revision = -1
        STATE.records_changed += 1
        STATE.download_workers = 0
        STATE.active_downloads = 0
        STATE.peak_downloads = 0
        STATE.downloaded_bytes = 0
        STATE.downloaded_files = 0
        STATE.download_started_clock = 0.0
        STATE.library_workers = 0
        STATE.active_library = 0
        STATE.peak_library = 0
    return {
        "archive": str(output),
        "cleared": [str(path) for path in archive_targets],
        "database": str(RUNTIME_DIR / "archive.sqlite3"),
    }


def auth_progress(message: str):
    translated = {
        "Waiting for scan": ("等待扫码", "请使用手机 QQ 扫描二维码", "waiting"),
        "Scanned; waiting for confirmation": ("已扫码，等待确认", "请在手机 QQ 上点击确认登录", "waiting"),
        "QR expired": ("二维码已过期", "正在自动刷新二维码", "waiting"),
    }
    status, detail, level = translated.get(message, (message, "", ""))
    STATE.update(login_status=status, login_detail=detail, login_level=level)


def complete_auth(session, uin: str, method: str):
    bundle = session.bundle(method, uin)
    STATE.update(
        session=session,
        uin=uin,
        api=QQGroupAlbumAPI(session, uin),
        login_status="登录成功",
        login_detail=cookie_summary(bundle),
        login_level="ok",
    )


def qr_auth_worker():
    try:
        while True:
            try:
                def show_qr(data: bytes):
                    STATE.update(qr="data:image/png;base64," + base64.b64encode(data).decode("ascii"))
                session, uin = qr_login(auth_progress, 180, show_qr)
                complete_auth(session, uin, "qr")
                return
            except TimeoutError:
                continue
    except Exception as exc:
        STATE.update(login_status="二维码登录失败", login_detail=str(exc), login_level="error")
    finally:
        STATE.update(auth_running=False)


def local_auth_worker():
    try:
        session, uin = local_qq_login(progress=auth_progress)
        complete_auth(session, uin, "local-qq")
    except Exception as exc:
        STATE.update(login_status="QQNT 快速登录失败", login_detail=str(exc), login_level="error")
    finally:
        STATE.update(auth_running=False)


def scan_worker(album_ids: list[str]):
    try:
        with STATE.lock:
            api = STATE.api
            group_id = STATE.group_id
            albums = [STATE.albums[value] for value in album_ids if value in STATE.albums]
        if api is None:
            raise RuntimeError("请先登录")
        total_expected = sum(album.photo_count for album in albums) or 1
        accumulated = 0
        for index, album in enumerate(albums, 1):
            if STATE.cancel_event.is_set():
                STATE.update(job_status=f"扫描已停止：已保存 {accumulated} 张记录")
                return
            STATE.update(job_status=f"扫描相册 {index}/{len(albums)}：{album.title}")
            records = api.all_photos(group_id, album)
            STATE.database.upsert(records)
            accumulated += len(records)
            STATE.update(progress=min(99, int(accumulated * 100 / total_expected)), records_changed=STATE.records_changed + 1)
        summary = STATE.rebuild_date_preview()
        STATE.update(
            job_status=f"元数据扫描完成：{accumulated} 张；日期预演覆盖 {summary['total']} 张",
            progress=100,
            records_changed=STATE.records_changed + 1,
        )
    except Exception as exc:
        STATE.update(job_status=f"扫描失败：{exc}")
    finally:
        STATE.update(job_running=False)


def download_analyze_worker(output: Path, workers: int = DEFAULT_DOWNLOAD_WORKERS):
    try:
        workers = max(1, min(int(workers), MAX_DOWNLOAD_WORKERS))
        records = [row for row in STATE.database.all() if not STATE.group_id or row.group_id == STATE.group_id]
        if not records:
            raise RuntimeError("请先扫描照片元数据")
        (output / "00_raw").mkdir(parents=True, exist_ok=True)
        from qq_album_archiver import Downloader
        downloader = Downloader(RUNTIME_DIR / "downloads")
        download_one = downloader.download_one
        analyzed = 0
        skipped = 0
        failed = 0
        cancelled = 0
        total = len(records)

        def process_record(record: PhotoRecord) -> str:
            if STATE.cancel_event.is_set():
                raise DownloadCancelled("Download cancelled")
            destination = raw_destination_for(record, output)
            existing_raw = Path(record.raw_path) if record.raw_path and Path(record.raw_path).exists() else None
            detected_existing = detect_media_suffix(existing_raw) if existing_raw else ""
            raw_matches = bool(existing_raw and detected_existing == existing_raw.suffix.lower())
            if record.media_kind == "video":
                raw_matches = raw_matches and detected_existing in VIDEO_EXTENSIONS
            elif record.media_kind == "image":
                raw_matches = raw_matches and detected_existing not in VIDEO_EXTENSIONS
            existing_complete = bool(existing_raw and raw_matches and media_file_is_complete(existing_raw))
            if existing_raw and existing_raw.resolve() == destination.resolve() and existing_complete:
                if record.status == "analyzed" and record.sha256 and record.analysis_version == FILE_ANALYSIS_VERSION:
                    return "skipped"
                analyze_raw_file(record, destination)
                return "reanalyzed"
            if existing_raw and (existing_raw.resolve() != destination.resolve() or not existing_complete):
                label = "__corrupt" if not existing_complete else "__legacy"
                legacy = existing_raw.with_name(existing_raw.stem + label + existing_raw.suffix)
                counter = 2
                while legacy.exists():
                    legacy = existing_raw.with_name(existing_raw.stem + f"{label}_{counter}" + existing_raw.suffix)
                    counter += 1
                existing_raw.replace(legacy)
                if record.local_path and Path(record.local_path).exists():
                    old_library = Path(record.local_path)
                    old_library_target = old_library.with_name(old_library.stem + label + old_library.suffix)
                    counter = 2
                    while old_library_target.exists():
                        old_library_target = old_library.with_name(old_library.stem + f"{label}_{counter}" + old_library.suffix)
                        counter += 1
                    old_library.replace(old_library_target)
                record.local_path = ""
                record.status = "pending"
            destination.parent.mkdir(parents=True, exist_ok=True)
            downloaded_bytes = 0
            STATE.begin_download()
            try:
                source = download_one(record, destination, cancel_event=STATE.cancel_event)
                downloaded_bytes = source.stat().st_size
            finally:
                STATE.finish_download(downloaded_bytes)
            if STATE.cancel_event.is_set():
                raise DownloadCancelled("Download cancelled")
            analyze_raw_file(record, source)
            return "analyzed"

        iterator = iter(records)
        pending: dict[object, PhotoRecord] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qq-photo") as executor:
            def submit_available() -> None:
                while len(pending) < workers and not STATE.cancel_event.is_set():
                    try:
                        record = next(iterator)
                    except StopIteration:
                        return
                    pending[executor.submit(process_record, record)] = record

            submit_available()
            while pending:
                if STATE.cancel_event.is_set():
                    for future in pending:
                        future.cancel()
                done, _ = wait(tuple(pending), timeout=0.25, return_when=FIRST_COMPLETED)
                if not done:
                    if STATE.cancel_event.is_set():
                        STATE.update(job_status=f"正在停止下载：等待 {len(pending)} 个线程退出")
                    continue
                for future in done:
                    record = pending.pop(future)
                    try:
                        result = future.result()
                        if result == "skipped":
                            skipped += 1
                        else:
                            analyzed += 1
                            STATE.database.update(record)
                    except (CancelledError, DownloadCancelled):
                        cancelled += 1
                    except Exception as exc:
                        record.status = "download_failed"
                        record.error = str(exc)
                        failed += 1
                        STATE.database.update(record)
                    finished = analyzed + skipped + failed
                    status_values = {
                        "job_status": (
                            f"正在停止下载：已完成 {finished}/{total}"
                            if STATE.cancel_event.is_set()
                            else f"并发下载与分析 {finished}/{total}（{workers} 线程）"
                        ),
                        "progress": int(finished * 100 / total),
                    }
                    if finished % 100 == 0:
                        status_values["records_changed"] = STATE.records_changed + 1
                    STATE.update(**status_values)
                submit_available()
        if STATE.cancel_event.is_set():
            STATE.update(job_status="下载线程已停止：正在保存已完成记录并重算日期")
        summary = STATE.rebuild_date_preview()
        STATE.database.export(output / "metadata")
        write_reports(STATE.database, output)
        prefix = "下载已停止" if STATE.cancel_event.is_set() else "下载分析完成"
        finished = analyzed + skipped + failed
        remaining = total - finished
        final_progress = int(finished * 100 / total)
        STATE.update(
            job_status=f"{prefix}：新增或重分析 {analyzed}，已存在 {skipped}，失败 {failed}，中断 {cancelled}，剩余 {remaining}；最终日期已重算 {summary['total']} 张",
            progress=100 if not STATE.cancel_event.is_set() else final_progress,
            records_changed=STATE.records_changed + 1,
        )
    except Exception as exc:
        STATE.update(job_status=f"下载分析失败：{exc}")
    finally:
        STATE.update(job_running=False)


def initialize_worker(output: Path):
    try:
        STATE.update(job_running=True,job_status="初始化：检查媒体类型、旧文件和日期索引",progress=10)
        STATE._repair_captured_metadata()
        STATE.update(job_status="初始化：重建 EXIF/QQ 日期匹配索引",progress=55)
        summary=STATE.rebuild_date_preview()
        STATE.database.export(output / "metadata")
        write_reports(STATE.database, output)
        STATE.update(job_status=f"初始化完成：{summary['total']} 条记录；未下载、未生成整理副本",progress=100,records_changed=STATE.records_changed+1)
    except Exception as exc:
        STATE.update(job_status=f"初始化失败：{exc}")
    finally:
        STATE.update(job_running=False)


def library_worker(output: Path, embed_metadata: bool = False, workers: int = DEFAULT_DOWNLOAD_WORKERS):
    try:
        records = [row for row in STATE.database.all() if not STATE.group_id or row.group_id == STATE.group_id]
        missing = [row for row in records if not row.raw_path or not Path(row.raw_path).exists()]
        if not records:
            STATE.update(job_status="没有可生成整理副本的记录", progress=100)
            return
        available = [row for row in records if row.raw_path and Path(row.raw_path).exists()]
        if not available:
            raise RuntimeError(f"仍有 {len(missing)} 张未下载分析，没有可生成的整理副本")
        workers = max(1, min(int(workers), MAX_DOWNLOAD_WORKERS))
        with STATE.lock:
            STATE.library_workers = workers
            STATE.active_library = 0
            STATE.peak_library = 0
        created = 0
        skipped = 0
        review = 0
        failed = 0
        cancelled = 0
        finished = 0

        def process_record(record: PhotoRecord) -> str:
            with STATE.lock:
                STATE.active_library += 1
                STATE.peak_library = max(STATE.peak_library, STATE.active_library)
            try:
                if STATE.cancel_event.is_set():
                    raise DownloadCancelled("Library task cancelled")
                if record.status == "archived" and record.local_path and Path(record.local_path).exists():
                    return "skipped"
                if record.date_review == "review" or record.date_source in {"UNKNOWN", "BATCH_SINGLE_DATE"}:
                    return "review"
                create_library_copy(record, output, embed_metadata=embed_metadata)
                return "created"
            finally:
                with STATE.lock:
                    STATE.active_library = max(0, STATE.active_library - 1)

        iterator = iter(available)
        pending: dict[object, PhotoRecord] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qq-exif") as executor:
            def submit_available() -> None:
                while len(pending) < workers and not STATE.cancel_event.is_set():
                    try:
                        record = next(iterator)
                    except StopIteration:
                        return
                    pending[executor.submit(process_record, record)] = record

            submit_available()
            while pending:
                if STATE.cancel_event.is_set():
                    for future in pending:
                        future.cancel()
                done, _ = wait(tuple(pending), timeout=0.25, return_when=FIRST_COMPLETED)
                if not done:
                    if STATE.cancel_event.is_set():
                        STATE.update(job_status=f"正在停止 EXIF 写入：等待 {len(pending)} 个线程退出")
                    continue
                for future in done:
                    record = pending.pop(future)
                    try:
                        result = future.result()
                        if result == "created":
                            created += 1
                            STATE.database.update(record)
                        elif result == "skipped":
                            skipped += 1
                        else:
                            review += 1
                    except (CancelledError, DownloadCancelled):
                        cancelled += 1
                    except Exception as exc:
                        record.status = "organize_failed"
                        record.error = str(exc)
                        failed += 1
                        STATE.database.update(record)
                    finished = created + skipped + review + failed + cancelled
                    STATE.update(
                        job_status=(
                            f"正在停止 EXIF 写入：已完成 {finished}/{len(available)}"
                            if STATE.cancel_event.is_set()
                            else f"并发写入整理副本 EXIF：已完成 {finished}/{len(available)}（{workers} 线程）"
                        ),
                        progress=int(finished * 100 / len(available)),
                        records_changed=STATE.records_changed + 1 if finished % 25 == 0 else STATE.records_changed,
                    )
                submit_available()
        STATE.database.export(output / "metadata")
        write_reports(STATE.database, output)
        prefix = "整理已停止" if STATE.cancel_event.is_set() else "整理完成"
        STATE.update(
            job_status=(
                f"{prefix}：生成 {created}，已有 {skipped}，待复核跳过 {review}，"
                f"失败 {failed}，未下载跳过 {len(missing)}，中断 {cancelled}（{workers} 线程）"
            ),
            progress=100 if not STATE.cancel_event.is_set() else int(finished * 100 / len(available)),
            records_changed=STATE.records_changed + 1,
        )
    except Exception as exc:
        STATE.update(job_status=f"整理失败：{exc}")
    finally:
        STATE.update(job_running=False)


class Handler(BaseHTTPRequestHandler):
    def json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(data)));self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(data)

    def body(self):
        size = int(self.headers.get("Content-Length", "0"));return json.loads(self.rfile.read(size) or b"{}")

    def bytes(self, data: bytes, content_type: str, filename: str = ""):
        self.send_response(200);self.send_header("Content-Type",content_type);self.send_header("Content-Length",str(len(data)));self.send_header("Cache-Control","no-store")
        if filename: self.send_header("Content-Disposition",f'attachment; filename="{filename}"')
        self.end_headers();self.wfile.write(data)

    def preview_rows(self, source_rows=None):
        source_rows = source_rows if source_rows is not None else STATE.records()
        rows = [row for row in source_rows if not STATE.group_id or row.group_id == STATE.group_id]
        parsed = urllib.parse.urlparse(self.path);params=urllib.parse.parse_qs(parsed.query)
        source=(params.get("source") or [""])[0];review=(params.get("review") or [""])[0];issue=(params.get("issue") or [""])[0];media=(params.get("media") or [""])[0];query=(params.get("q") or [""])[0].strip().lower()
        if source: rows=[row for row in rows if row.date_source==source]
        if review: rows=[row for row in rows if row.date_review==review]
        if issue == "review": rows=[row for row in rows if row.date_review=="review"]
        elif issue == "organize_failed": rows=[row for row in rows if row.status=="organize_failed"]
        elif issue == "download_failed": rows=[row for row in rows if row.status=="download_failed"]
        elif issue == "pending": rows=[row for row in rows if not row.raw_path or not Path(row.raw_path).exists()]
        if media == "video": rows=[row for row in rows if row.media_kind == "video"]
        elif media == "image": rows=[row for row in rows if row.media_kind != "video"]
        if query:
            rows=[row for row in rows if query in " ".join((row.album_name,row.original_name,row.photo_id,row.uploader,row.camera_model,row.date_note)).lower()]
        return rows

    def do_GET(self):  # noqa: N802
        route = urllib.parse.urlparse(self.path).path
        if route == "/":
            data = PAGE.encode("utf-8");self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
        elif route == "/favicon.ico":
            self.send_response(204);self.end_headers()
        elif route == "/api/status": self.json(STATE.snapshot())
        elif route == "/api/records":
            source_rows=STATE.records();rows=self.preview_rows(source_rows);params=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query);limit=min(5000,max(1,int((params.get("limit") or ["3000"])[0])));offset=max(0,int((params.get("offset") or ["0"])[0]));view=(params.get("view") or ["summary"])[0];names=tuple(field for field in asdict(PhotoRecord()).keys() if field!="raw_json") if view=="full" else SUMMARY_RECORD_FIELDS
            all_rows=[row for row in source_rows if not STATE.group_id or row.group_id==STATE.group_id];self.json({"total":len(rows),"with_upload_time":sum(bool(row.upload_time) for row in rows),"preview_summary":STATE.preview_summary,"stage_summary":STATE.stage_summary(all_rows),"rows":[{name:getattr(row,name) for name in names} for row in rows[offset:offset+limit]]})
        elif route == "/api/record":
            params=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query);key=(params.get("key") or [""])[0];include_raw=(params.get("raw") or [""])[0] == "1"
            row=STATE.database.get(key)
            if row is None: self.json({"ok":False,"message":"照片记录不存在"},404)
            else:
                payload=asdict(row)
                if not include_raw: payload["raw_json"]=""
                self.json(payload)
        elif route == "/api/date-preview.csv":
            rows=self.preview_rows();buffer=io.StringIO();names=[field for field in asdict(PhotoRecord()).keys() if field!="raw_json"];writer=csv.DictWriter(buffer,fieldnames=names);writer.writeheader();writer.writerows({name:getattr(row,name) for name in names} for row in rows);self.bytes(buffer.getvalue().encode("utf-8-sig"),"text/csv; charset=utf-8","qq-date-preview.csv")
        else: self.send_error(404)

    def do_POST(self):  # noqa: N802
        if self.path in {"/api/login/qr", "/api/login/local"}:
            if STATE.auth_running: self.json({"ok":False,"message":"登录任务正在运行"},409);return
            STATE.update(auth_running=True,login_status="正在建立登录会话",login_level="waiting")
            target = qr_auth_worker if self.path.endswith("qr") else local_auth_worker
            threading.Thread(target=target,daemon=True).start();self.json({"ok":True});return
        if self.path == "/api/albums":
            group = str(self.body().get("group", "")).strip()
            if not group.isdigit(): self.json({"ok":False,"message":"QQ群号应当只包含数字"},400);return
            with STATE.lock: api=STATE.api
            if api is None: self.json({"ok":False,"message":"请先登录"},401);return
            try:
                albums=api.list_albums(group);STATE.update(group_id=group,albums={a.album_id:a for a in albums},records_changed=STATE.records_changed+1);self.json({"ok":True,"albums":[{"album_id":a.album_id,"title":a.title,"photo_count":a.photo_count} for a in albums]})
            except Exception as exc: self.json({"ok":False,"message":str(exc)},500)
            return
        if self.path == "/api/reset":
            body=self.body();confirmation=str(body.get("confirm",""));output=str(body.get("output","")).strip() or str(DEFAULT_ARCHIVE)
            if confirmation != RESET_CONFIRMATION: self.json({"ok":False,"message":"确认词不正确，未执行清空"},400);return
            if not output: self.json({"ok":False,"message":"请输入归档目录"},400);return
            path=Path(output).expanduser()
            if not path.is_absolute(): self.json({"ok":False,"message":"归档目录必须是绝对路径"},400);return
            try:
                result=reset_local_data(path)
                self.json({"ok":True,"message":"本地数据已彻底清空，可以重新登录并下载","details":result})
            except RuntimeError as exc: self.json({"ok":False,"message":str(exc)},409)
            except Exception as exc: self.json({"ok":False,"message":f"清空失败：{exc}"},500)
            return
        if self.path == "/api/initialize":
            if STATE.job_running: self.json({"ok":False,"message":"已有任务正在运行"},409);return
            body=self.body();output=str(body.get("output","")).strip() or str(DEFAULT_ARCHIVE)
            path=Path(output).expanduser()
            if not path.is_absolute(): self.json({"ok":False,"message":"归档目录必须是绝对路径"},400);return
            STATE.cancel_event.clear();STATE.update(job_running=True,job_status="准备初始化：不会下载，也不会生成整理副本",progress=0)
            threading.Thread(target=initialize_worker,args=(path,),daemon=True).start()
            self.json({"ok":True,"message":"初始化已开始：只检查媒体、EXIF 和日期索引"});return
        if self.path == "/api/scan":
            ids=[str(value) for value in self.body().get("album_ids",[])];
            if STATE.job_running: self.json({"ok":False,"message":"已有任务正在运行"},409);return
            if not ids: self.json({"ok":False,"message":"请至少选择一个相册"},400);return
            STATE.cancel_event.clear();STATE.update(job_running=True,job_status="开始扫描",progress=0);threading.Thread(target=scan_worker,args=(ids,),daemon=True).start();self.json({"ok":True,"message":"元数据扫描已开始"});return
        if self.path == "/api/review":
            if STATE.job_running: self.json({"ok":False,"message":"请等待当前任务结束后再复核"},409);return
            body=self.body();key=str(body.get("key","")).strip();action=str(body.get("action","manual")).strip().lower();record=STATE.database.get(key)
            if record is None: self.json({"ok":False,"message":"照片记录不存在"},404);return
            try:
                previous=(record.date_source or "UNKNOWN", record.date_review or "review", bool(record.date_conflict))
                if action in {"accept","manual"}:
                    chosen=str(body.get("chosen_date","")).strip()
                    if not chosen: self.json({"ok":False,"message":"请先选择确认日期"},400);return
                    apply_manual_review(record,chosen,str(body.get("note","")).strip(),accept_suggestion=action=="accept")
                    message="已确认当前建议日期" if action=="accept" else "已保存人工指定日期"
                elif action == "keep":
                    record.manual_reviewed="";record.reviewed_at="";record.reviewed_from_source="";record.review_note=""
                    message="已保留为待复核"
                else:
                    self.json({"ok":False,"message":"未知复核操作"},400);return
                STATE.database.update(record)
                STATE.record_changed(record, previous, message)
                payload=asdict(record);payload["raw_json"]=""
                self.json({"ok":True,"message":message,"record":payload});return
            except ValueError as exc:
                self.json({"ok":False,"message":str(exc)},400);return
            except Exception as exc:
                self.json({"ok":False,"message":f"复核保存失败：{exc}"},500);return
        if self.path == "/api/review-bulk":
            if STATE.job_running: self.json({"ok":False,"message":"请等待当前任务结束后再批量复核"},409);return
            body=self.body();action=str(body.get("action","accept")).strip().lower()
            if action != "accept": self.json({"ok":False,"message":"批量操作目前只支持接受当前建议"},400);return
            try:
                rows=[row for row in STATE.records() if (not STATE.group_id or row.group_id==STATE.group_id) and row.date_review=="review" and row.manual_reviewed!="yes"]
                accepted=0;skipped=0;source_deltas={};review_deltas={}
                for record in rows:
                    if not record.chosen_time:
                        skipped+=1;continue
                    try:
                        old_note=record.date_note
                        old_source=record.date_source or "UNKNOWN";old_review=record.date_review or "review"
                        note="批量接受当前建议"
                        if old_note: note += "；原说明：" + old_note
                        apply_manual_review(record,record.chosen_time,note,accept_suggestion=True)
                        STATE.database.update(record);accepted+=1
                        source_deltas[old_source]=source_deltas.get(old_source,0)-1;source_deltas["MANUAL_REVIEW"]=source_deltas.get("MANUAL_REVIEW",0)+1
                        review_deltas[old_review]=review_deltas.get(old_review,0)-1;review_deltas["ready"]=review_deltas.get("ready",0)+1
                    except ValueError:
                        skipped+=1
                remaining_review=sum(row.date_review=="review" for row in rows)
                with STATE.lock:
                    summary=dict(STATE.preview_summary or {})
                    sources=dict(summary.get("sources") or {});reviews=dict(summary.get("reviews") or {})
                    for key,delta in source_deltas.items(): sources[key]=int(sources.get(key,0))+delta
                    for key,delta in review_deltas.items(): reviews[key]=int(reviews.get(key,0))+delta
                    summary["sources"]={key:value for key,value in sources.items() if value};summary["reviews"]={key:value for key,value in reviews.items() if value}
                    STATE.preview_summary=summary;STATE.records_changed+=1;STATE._records_cache_revision=-1
                message=f"批量复核完成：接受 {accepted} 条，跳过 {skipped} 条；剩余待复核 {remaining_review} 条"
                STATE.update(job_status=message)
                self.json({"ok":True,"message":message,"accepted":accepted,"skipped":skipped,"remaining_review":remaining_review,"summary":summary});return
            except Exception as exc:
                self.json({"ok":False,"message":f"批量复核失败：{exc}"},500);return
        if self.path == "/api/preview":
            if STATE.job_running: self.json({"ok":False,"message":"请等待当前任务结束"},409);return
            summary=STATE.rebuild_date_preview();self.json({"ok":True,"message":f"日期预演完成：{summary['total']} 张，{summary['conflicts']} 个冲突", "summary":summary});return
        if self.path in {"/api/download-analyze", "/api/library"}:
            body=self.body();output=str(body.get("output","")).strip();embed=bool(body.get("embed",False))
            try: workers=max(1,min(MAX_DOWNLOAD_WORKERS,int(body.get("workers",DEFAULT_DOWNLOAD_WORKERS) or DEFAULT_DOWNLOAD_WORKERS)))
            except (TypeError,ValueError): workers=DEFAULT_DOWNLOAD_WORKERS
            if STATE.job_running: self.json({"ok":False,"message":"已有任务正在运行"},409);return
            if not output: self.json({"ok":False,"message":"请输入归档目录"},400);return
            path=Path(output).expanduser()
            if not path.is_absolute(): self.json({"ok":False,"message":"归档目录必须是绝对路径"},400);return
            if self.path == "/api/library" and embed and not STATE.exiftool_available: self.json({"ok":False,"message":"未检测到 ExifTool，无法内嵌日期"},400);return
            STATE.cancel_event.clear()
            if self.path == "/api/download-analyze":
                STATE.reset_download_metrics(workers);STATE.update(job_running=True,job_status=f"准备以 {workers} 个线程下载原图并分析",progress=0);threading.Thread(target=download_analyze_worker,args=(path,workers),daemon=True).start();self.json({"ok":True,"message":f"下载与分析任务已开始：{workers} 个线程；不会生成整理副本"});return
            rows=[row for row in STATE.records() if not STATE.group_id or row.group_id==STATE.group_id]
            missing=sum(not row.raw_path or not Path(row.raw_path).exists() for row in rows)
            available=len(rows)-missing
            if not available:
                self.json({"ok":False,"message":f"仍有 {missing} 张未下载分析，没有可生成的整理副本"},409);return
            review=sum(row.date_review=="review" or row.date_source in {"UNKNOWN","BATCH_SINGLE_DATE"} for row in rows if row.raw_path and Path(row.raw_path).exists())
            STATE.update(job_running=True,job_status=f"准备以 {workers} 个线程生成整理副本",progress=0);threading.Thread(target=library_worker,args=(path,embed,workers),daemon=True).start();self.json({"ok":True,"message":f"整理任务已开始：{workers} 个线程；{review} 张待复核照片会被跳过；{missing} 张未下载记录将保留"});return
        if self.path == "/api/archive":
            self.json({"ok":False,"message":"流程已拆分，请先下载并分析，再生成整理副本"},410);return
        if self.path == "/api/cancel":
            if not STATE.job_running: self.json({"ok":False,"message":"当前没有运行中的任务"},409);return
            STATE.cancel_event.set();STATE.update(job_status="正在停止任务：下载线程会中断传输并清理未完成文件");self.json({"ok":True,"message":"已请求停止；正在传输的文件会中断，未完成文件不会保留"});return
        if self.path == "/api/shutdown":
            if STATE.job_running: self.json({"ok":False,"message":"请先停止当前任务，再关闭后台服务"},409);return
            if STATE.auth_running: self.json({"ok":False,"message":"请等待登录任务结束，再关闭后台服务"},409);return
            self.json({"ok":True,"message":"后台服务正在关闭"})
            server = HTTP_SERVER
            if server is not None:
                threading.Thread(target=server.shutdown, name="qq-server-shutdown", daemon=True).start()
            return
        self.send_error(404)

    def log_message(self,*_args): return


def main():
    global HTTP_SERVER
    parser=argparse.ArgumentParser();parser.add_argument("--port",type=int,default=8767);args=parser.parse_args();server=ThreadingHTTPServer(("127.0.0.1",args.port),Handler);HTTP_SERVER=server;print(f"QQ Album Archiver: http://127.0.0.1:{args.port}/",flush=True)
    if getattr(sys, "frozen", False):
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    HTTP_SERVER = None


if __name__ == "__main__": main()
