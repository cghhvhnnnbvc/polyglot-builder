# -*- coding: utf-8 -*-
"""资源台账模块 - JSON 数据源 + 自动生成的 HTML 查看页。

存储设计:
  - **JSON 是唯一数据源** (`资源台账.json`): `{"version": 1, "records": [...]}`，
    标准格式、原子写入 (临时文件 + os.replace)、可 diff/备份/迁移。
  - **HTML 查看页是衍生物** (`资源台账.html`, 同目录同名): 每次保存由 JSON 重新生成,
    自包含 (数据内嵌), 双击浏览器即可搜索/筛选/复制/导出 CSV; 删了也能随时重建。
    数据必须内嵌而非外链: 本地 file:// 页面用 fetch 读同目录文件会被浏览器拦截。
  - 分离的好处: 读取不再靠正则从 HTML 里抠数据、不用转义 `</`;
    查看器模板升级后, 旧数据下次保存就自动用新界面渲染。
  - **旧版单文件 HTML 台账自动迁移**: 读数据 → 写 JSON → 旧 HTML 改名 .bak → 生成新查看页。
  - 密码为**明文**存储 (用户已确认): 台账文件只留在本地, 切勿上传网盘或分享。
  - 加密由 WinRAR 层负责, 本工具与本模块均不参与加解密, 台账仅作记录。
"""
from __future__ import annotations

import json
import os
import re
import sys
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# 台账数据文件 (唯一数据源, 与程序同目录); HTML 查看页与其同名同目录
LEDGER_FILENAME = '资源台账.json'
LEDGER_SCHEMA_VERSION = 1

# HTML 查看页中的数据块标记 (生成与旧版迁移共用)
_DATA_MARKER = '__LEDGER_DATA__'
_DATA_RE = re.compile(
    r'(<script id="ledger-data" type="application/json">)(.*?)(</script>)',
    re.DOTALL)


class LedgerError(Exception):
    """台账文件读写错误 (缺失/损坏/格式不符)。"""


@dataclass
class LedgerRecord:
    """一条资源目录记录。

    字段分两类:
      - 构建时自动预填: filename / size / date
      - 用户手工补充:   name / netdisk / netdisk_path / share_link /
                        share_code / rar_password / note
    """
    name: str = ''            # 资源名称/说明 (这是什么资源)
    filename: str = ''        # 构建产物文件名
    size: str = ''            # 文件大小 (人类可读)
    date: str = ''            # 记录时间 YYYY-MM-DD HH:MM
    netdisk: str = ''         # 网盘平台 (百度网盘/夸克网盘/...)
    netdisk_path: str = ''    # 网盘中的位置 (目录路径)
    share_link: str = ''      # 分享链接
    share_code: str = ''      # 分享提取码
    rar_password: str = ''    # RAR 密码 (WinRAR 解压用, 明文记录)
    note: str = ''            # 备注


# 字段顺序 = HTML 表格列顺序 (name 在最前, 便于快速识别资源)
FIELD_LABELS = [
    ('name', '资源名称'),
    ('filename', '文件名'),
    ('size', '大小'),
    ('date', '记录时间'),
    ('netdisk', '网盘'),
    ('netdisk_path', '网盘位置'),
    ('share_link', '分享链接'),
    ('share_code', '提取码'),
    ('rar_password', 'RAR 密码'),
    ('note', '备注'),
]

_KEYS = [k for k, _ in FIELD_LABELS]

# 常见网盘平台 (HTML 筛选下拉与 GUI 下拉共用)
NETDISK_CHOICES = ['百度网盘', '夸克网盘', '阿里云盘', '迅雷云盘', '115网盘',
                   '天翼云盘', 'OneDrive', 'Google Drive', '其他']


def now_str() -> str:
    """当前时间字符串 (台账记录用)。"""
    return datetime.now().strftime('%Y-%m-%d %H:%M')


def default_ledger_path() -> str:
    """默认台账数据文件路径: 打包版取 exe 所在目录, 源码运行取脚本目录。"""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, LEDGER_FILENAME)


def normalize_ledger_path(path: str) -> str:
    """把任意台账路径规范化为 JSON 数据文件路径。

    传入 .html (旧版单文件台账 / 旧配置 / 旧命令行参数) 时返回同 stem 的 .json;
    其余情况原样返回。本函数不碰磁盘, 迁移由 migrate_legacy_html() 按需执行。
    """
    root, ext = os.path.splitext(path)
    if ext.lower() == '.html':
        return root + '.json'
    return path


def html_view_path(path: str) -> str:
    """台账数据文件对应的 HTML 查看页路径 (同目录同名, 后缀 .html)。"""
    return os.path.splitext(normalize_ledger_path(path))[0] + '.html'


def _legacy_html_path(json_path: str) -> str:
    """与 JSON 同 stem 的 HTML 路径 (可能是旧版单文件台账)。"""
    return os.path.splitext(json_path)[0] + '.html'


def _records_from_json_text(raw: str) -> List[LedgerRecord]:
    """从 JSON 文本解析记录列表 (兼容新版 dict 与旧版数组)。"""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LedgerError(f'台账数据损坏, 无法解析: {e}') from e
    if isinstance(data, dict):
        data = data.get('records', [])
    if not isinstance(data, list):
        raise LedgerError('台账数据格式不正确 (应为记录列表)。')

    records: List[LedgerRecord] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        records.append(LedgerRecord(**{k: str(item.get(k, '')) for k in _KEYS}))
    return records


def _read_legacy_html(html_path: str) -> List[LedgerRecord]:
    """从旧版单文件 HTML 台账中解析数据块 (仅用于迁移)。"""
    try:
        with open(html_path, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        raise LedgerError(f'无法读取旧版台账文件: {e}') from e

    m = _DATA_RE.search(text)
    if not m:
        raise LedgerError(
            f'台账文件格式不正确 (未找到数据块), 可能不是本工具生成的: {html_path}')
    return _records_from_json_text(m.group(2))


def _write_json(json_path: str, records: List[LedgerRecord]) -> None:
    """原子写入 JSON 数据文件 (先写临时文件再 os.replace, 避免写一半损坏)。"""
    payload = {'version': LEDGER_SCHEMA_VERSION,
               'records': [asdict(r) for r in records]}
    tmp = json_path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, json_path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise LedgerError(f'无法写入台账数据文件: {e}') from e


def _write_html_view(json_path: str, records: List[LedgerRecord]) -> None:
    """由 JSON 数据生成只读 HTML 查看页 (衍生物, 可随时重建)。"""
    payload = json.dumps([asdict(r) for r in records],
                         ensure_ascii=False, indent=2)
    # 查看页中 JSON 内的 "</" 会提前闭合 <script>, 转义为 "<\/" (JSON 合法转义)
    payload = payload.replace('</', '<\\/')
    html = _HTML_TEMPLATE.replace(_DATA_MARKER, payload)
    try:
        with open(html_view_path(json_path), 'w', encoding='utf-8') as f:
            f.write(html)
    except OSError as e:
        raise LedgerError(f'无法生成台账查看页: {e}') from e


def migrate_legacy_html(path: str) -> Optional[str]:
    """旧版单文件 HTML 台账 → JSON + 新查看页。

    仅当 JSON 不存在且同 stem 的 HTML 存在时执行:
    读旧 HTML 数据 → 写 JSON → 旧 HTML 改名 .bak → 生成新 HTML 查看页。
    返回迁移后的 JSON 路径; 无需迁移时返回 None。
    """
    json_path = normalize_ledger_path(path)
    if os.path.isfile(json_path):
        return None
    html_path = _legacy_html_path(json_path)
    if not os.path.isfile(html_path):
        return None

    records = _read_legacy_html(html_path)
    _write_json(json_path, records)
    try:
        os.replace(html_path, html_path + '.bak')
    except OSError:
        pass  # 改名失败不影响数据 (JSON 已写定), 新查看页仍会覆盖生成
    _write_html_view(json_path, records)
    return json_path


def load_records(path: str) -> List[LedgerRecord]:
    """读取台账中的全部记录 (必要时先自动迁移旧版 HTML 台账)。

    文件不存在或数据损坏时抛 LedgerError (由调用方决定如何提示)。
    """
    json_path = normalize_ledger_path(path)
    migrate_legacy_html(json_path)
    if not os.path.isfile(json_path):
        raise LedgerError(f'台账文件不存在: {json_path}')
    try:
        with open(json_path, encoding='utf-8') as f:
            raw = f.read()
    except OSError as e:
        raise LedgerError(f'无法读取台账文件: {e}') from e
    return _records_from_json_text(raw)


def save_records(path: str, records: List[LedgerRecord]) -> None:
    """写入 JSON 数据文件, 并重新生成 HTML 查看页。"""
    json_path = normalize_ledger_path(path)
    _write_json(json_path, records)
    _write_html_view(json_path, records)


def create_ledger(path: str) -> None:
    """创建一个空台账 (JSON 数据文件 + HTML 查看页)。"""
    save_records(path, [])


def append_record(path: str, record: LedgerRecord) -> None:
    """追加一条记录 (台账不存在时自动创建; 旧版 HTML 台账先迁移)。"""
    json_path = normalize_ledger_path(path)
    if (os.path.isfile(json_path)
            or os.path.isfile(_legacy_html_path(json_path))):
        records = load_records(json_path)
    else:
        records = []
    records.append(record)
    save_records(json_path, records)


def open_ledger(path: str) -> None:
    """用系统默认浏览器打开 HTML 查看页 (缺失则由 JSON 重新生成)。"""
    json_path = normalize_ledger_path(path)
    view = html_view_path(json_path)
    if not os.path.isfile(view):
        _write_html_view(json_path, load_records(json_path))
    webbrowser.open(Path(view).resolve().as_uri())


# ============================================================
# 台账位置记忆: 用户选过一次位置后, 下次启动直接沿用
# ============================================================
_CONFIG_FILENAME = 'ledger_config.json'


def config_path() -> str:
    """位置记忆文件路径 (与程序同目录)。"""
    return os.path.join(os.path.dirname(default_ledger_path()), _CONFIG_FILENAME)


def load_configured_path() -> Optional[str]:
    """读取用户上次选择的台账路径 (规范化为 JSON); 无记录或已失效时返回 None。

    兼容旧配置: 存的可能是旧版 .html 路径, 这里规范化为 .json;
    只要 JSON 或待迁移的旧 HTML 存在, 就视为有效。
    """
    cfg = config_path()
    if not os.path.isfile(cfg):
        return None
    try:
        with open(cfg, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    path = data.get('ledger_path') if isinstance(data, dict) else None
    if not isinstance(path, str) or not path:
        return None
    json_path = normalize_ledger_path(path)
    if (os.path.isfile(json_path)
            or os.path.isfile(_legacy_html_path(json_path))):
        return json_path
    return None


def save_configured_path(path: str) -> None:
    """记住台账路径 (存规范化后的 JSON 路径; 写入失败静默忽略)。"""
    try:
        with open(config_path(), 'w', encoding='utf-8') as f:
            json.dump({'ledger_path':
                       os.path.abspath(normalize_ledger_path(path))},
                      f, ensure_ascii=False)
    except OSError:
        pass


def resolve_ledger_path() -> str:
    """当前应使用的台账数据文件路径: 优先用户记住的位置, 否则默认位置。"""
    return load_configured_path() or default_ledger_path()


# ============================================================
# 自包含 HTML 查看器模板
# ============================================================
_HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>资源台账 - Polyglot Builder</title>
<style>
  :root {
    --bg: #f6f7f9; --card: #fff; --line: #e3e6ea; --text: #1f2328;
    --sec: #6b7280; --accent: #1e88e5; --accent-d: #0d47a1; --warn: #b26a00;
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; background: var(--bg); color: var(--text);
         font: 14px/1.6 "Segoe UI", "Microsoft YaHei", sans-serif; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: var(--sec); font-size: 13px; margin-bottom: 18px; }
  .toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
             background: var(--card); border: 1px solid var(--line);
             border-radius: 10px; padding: 12px 14px; margin-bottom: 16px; }
  .toolbar input[type=search], .toolbar select {
      padding: 7px 10px; border: 1px solid var(--line); border-radius: 8px;
      font-size: 14px; background: #fff; color: var(--text); }
  .toolbar input[type=search] { flex: 1 1 240px; min-width: 180px; }
  .count { color: var(--sec); font-size: 13px; margin-left: auto; }
  button { padding: 7px 12px; border: 1px solid var(--line); border-radius: 8px;
           background: #fff; color: var(--text); font-size: 13px; cursor: pointer; }
  button:hover { border-color: var(--accent); color: var(--accent); }
  .tablewrap { background: var(--card); border: 1px solid var(--line);
               border-radius: 10px; overflow: auto; }
  table { width: 100%; border-collapse: collapse; min-width: 1080px; }
  th, td { padding: 10px 12px; border-bottom: 1px solid var(--line);
           text-align: left; vertical-align: top; white-space: nowrap; }
  th { background: #fafbfc; font-size: 13px; color: var(--sec); font-weight: 600;
       position: sticky; top: 0; }
  tbody tr:hover { background: #f8fbff; }
  td.wrap { white-space: normal; min-width: 160px; max-width: 320px;
            word-break: break-all; }
  .pw { font-family: Consolas, monospace; letter-spacing: 1px; }
  .cellbtns { display: inline-flex; gap: 4px; margin-left: 6px; }
  .cellbtns button { padding: 2px 7px; font-size: 12px; border-radius: 6px; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .empty { padding: 48px 16px; text-align: center; color: var(--sec); }
  .notice { margin-top: 16px; padding: 10px 14px; border-radius: 8px;
            background: #fff8e6; border: 1px solid #f0dca8; color: var(--warn);
            font-size: 13px; }
  .idx { color: var(--sec); }
</style>
</head>
<body>
<h1>资源台账</h1>
<div class="sub">Polyglot Builder 生成的资源目录 · 记录资源、网盘位置与 RAR 密码</div>

<div class="toolbar">
  <input type="search" id="q" placeholder="搜索资源名称 / 文件名 / 网盘位置 / 备注 ...">
  <select id="disk"><option value="">全部网盘</option></select>
  <button id="csv">导出 CSV</button>
  <span class="count" id="count"></span>
</div>

<div class="tablewrap">
  <table>
    <thead><tr id="head"></tr></thead>
    <tbody id="body"></tbody>
  </table>
  <div class="empty" id="empty" style="display:none">暂无记录 — 在 Polyglot Builder 中构建成功后选择"记入台账"即可添加。</div>
</div>

<div class="notice">
  ⚠️ 本文件中的 RAR 密码为<b>明文存储</b>，请仅保存在本地，切勿上传网盘、截图或发送给他人。
  加密由 WinRAR 负责，本台账仅作记录用途。
</div>

<script id="ledger-data" type="application/json">__LEDGER_DATA__</script>
<script>
var LABELS = [["name","资源名称"],["filename","文件名"],["size","大小"],
              ["date","记录时间"],["netdisk","网盘"],["netdisk_path","网盘位置"],
              ["share_link","分享链接"],["share_code","提取码"],
              ["rar_password","RAR 密码"],["note","备注"]];
var KEYS = LABELS.map(function (p) { return p[0]; });
var WRAP = {netdisk_path: 1, share_link: 1, note: 1, name: 1};

function loadData() {
  try {
    var raw = document.getElementById('ledger-data').textContent.trim();
    var arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch (e) { return []; }
}
var DATA = loadData();

function copyText(text, btn) {
  function done() {
    if (!btn) return;
    var old = btn.textContent;
    btn.textContent = '已复制';
    setTimeout(function () { btn.textContent = old; }, 1200);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function () { fallback(); });
  } else { fallback(); }
  function fallback() {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } catch (e) {}
    document.body.removeChild(ta);
  }
}

function pwCell(rec, tr) {
  var td = document.createElement('td');
  var span = document.createElement('span');
  span.className = 'pw';
  var shown = false;
  function paint() {
    span.textContent = rec.rar_password
      ? (shown ? rec.rar_password : '•'.repeat(Math.min(rec.rar_password.length, 10)))
      : '—';
  }
  paint();
  td.appendChild(span);
  if (rec.rar_password) {
    var box = document.createElement('span');
    box.className = 'cellbtns';
    var eye = document.createElement('button');
    eye.textContent = '显示';
    eye.onclick = function () {
      shown = !shown; paint(); eye.textContent = shown ? '隐藏' : '显示';
    };
    var cp = document.createElement('button');
    cp.textContent = '复制';
    cp.onclick = function () { copyText(rec.rar_password, cp); };
    box.appendChild(eye); box.appendChild(cp);
    td.appendChild(box);
  }
  return td;
}

function render() {
  var q = document.getElementById('q').value.trim().toLowerCase();
  var disk = document.getElementById('disk').value;
  var head = document.getElementById('head');
  var body = document.getElementById('body');
  head.innerHTML = ''; body.innerHTML = '';

  LABELS.forEach(function (p) {
    var th = document.createElement('th'); th.textContent = p[1]; head.appendChild(th);
  });
  var thOp = document.createElement('th'); thOp.textContent = '操作'; head.appendChild(thOp);

  var rows = DATA.filter(function (r) {
    if (disk && (r.netdisk || '') !== disk) return false;
    if (!q) return true;
    return KEYS.some(function (k) {
      return String(r[k] || '').toLowerCase().indexOf(q) >= 0;
    });
  });

  rows.forEach(function (r, i) {
    var tr = document.createElement('tr');
    LABELS.forEach(function (p) {
      var k = p[0], td;
      if (k === 'rar_password') { td = pwCell(r, tr); }
      else {
        td = document.createElement('td');
        if (WRAP[k]) td.className = 'wrap';
        if (k === 'share_link' && r[k]) {
          var a = document.createElement('a');
          a.href = r[k]; a.target = '_blank'; a.rel = 'noreferrer';
          a.textContent = r[k]; td.appendChild(a);
        } else { td.textContent = r[k] || '—'; }
      }
      tr.appendChild(td);
    });
    var op = document.createElement('td');
    var box = document.createElement('span'); box.className = 'cellbtns';
    if (r.share_code) {
      var b1 = document.createElement('button');
      b1.textContent = '复制提取码';
      b1.onclick = function () { copyText(r.share_code, b1); };
      box.appendChild(b1);
    }
    var b2 = document.createElement('button');
    b2.textContent = '复制文件名';
    b2.onclick = function () { copyText(r.filename, b2); };
    box.appendChild(b2);
    op.appendChild(box);
    tr.appendChild(op);
    body.appendChild(tr);
  });

  document.getElementById('empty').style.display = rows.length ? 'none' : 'block';
  document.getElementById('count').textContent =
    '共 ' + DATA.length + ' 条' + (rows.length === DATA.length ? '' : ' (筛出 ' + rows.length + ' 条)');
}

function initDisks() {
  var sel = document.getElementById('disk');
  var seen = {};
  DATA.forEach(function (r) { if (r.netdisk) seen[r.netdisk] = 1; });
  Object.keys(seen).sort().forEach(function (d) {
    var o = document.createElement('option'); o.value = d; o.textContent = d; sel.appendChild(o);
  });
}

function exportCsv() {
  var lines = [LABELS.map(function (p) { return p[1]; }).join(',')];
  DATA.forEach(function (r) {
    lines.push(KEYS.map(function (k) {
      var v = String(r[k] == null ? '' : r[k]);
      return '"' + v.replace(/"/g, '""') + '"';
    }).join(','));
  });
  var blob = new Blob(['\ufeff' + lines.join('\r\n')], {type: 'text/csv;charset=utf-8'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '资源台账.csv';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
}

document.getElementById('q').addEventListener('input', render);
document.getElementById('disk').addEventListener('change', render);
document.getElementById('csv').addEventListener('click', exportCsv);
initDisks();
render();
</script>
</body>
</html>
'''
