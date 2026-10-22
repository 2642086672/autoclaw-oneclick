# -*- coding: utf-8 -*-
"""
persona_extract.py —— 从 AutoClaw 客户端抽取闸门所需的 persona（system prompt）

为什么单独写一份：仓库自带的 extract_persona.py 只认 2.0.1 的
`ZWORK_DEFAULT_SYSTEM_PROMPT`；2.2.0 改成了 `Qen()` 函数拼装，脚本直接报
"找不到声明"。本脚本两条路都走：

  A 路（2.0.1）：找 ZWORK_DEFAULT_SYSTEM_PROMPT / ZWORK_LEGAL_VERTICAL_SYSTEM_PROMPT
  B 路（2.2.0）：找 Qen() / Uni 的拼装常量，把它们抠出来交给 node 求值

两路都失败时返回 False，由上层提示人工处理。

用法：
  python persona_extract.py --out <path>          抽取并写出
  python persona_extract.py --check --out <path>  体检（0=一致 3=不一致 1=失败）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

MAIN_JS = "main.cjs"
IDENT_RE = re.compile(r"[A-Za-z0-9_]+")


def locate_client():
    """定位客户端主 bundle。找不到抛 RuntimeError。"""
    env = os.environ.get("AUTOCLAW_BUNDLE")
    if env and os.path.isfile(env):
        return Path(env)
    roots = []
    pf = os.environ.get("ProgramFiles", "C:\\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
    lad = os.environ.get("LOCALAPPDATA", "")
    roots += [pf, pf86, os.path.join(lad, "Programs") if lad else "", "C:\\", "D:\\", "E:\\"]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if "autoclaw" not in name.lower():
                continue
            base = os.path.join(root, name)
            # 安装目录可能是 <root>\AutoClaw2，也可能是 <root>\AutoClaw\App —— 扫两层
            cands = [base]
            try:
                for sub in os.listdir(base):
                    if os.path.isdir(os.path.join(base, sub)):
                        cands.append(os.path.join(base, sub))
            except OSError:
                pass
            # 两遍扫描：优先取未打包的 main.cjs（可直接读），其次才是 asar
            for rel in (os.path.join("resources", "app", "dist", MAIN_JS),
                        os.path.join("resources", "app.asar")):
                for c in cands:
                    p = os.path.join(c, rel)
                    if os.path.isfile(p):
                        return Path(p)
    # 注册表兜底
    try:
        import winreg
        for hive, path in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        ):
            try:
                key = winreg.OpenKey(hive, path)
            except OSError:
                continue
            with key:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(key, sub) as sk:
                            loc, _ = winreg.QueryValueEx(sk, "InstallLocation")
                    except OSError:
                        continue
                    p = os.path.join(loc, "resources", "app", "dist", MAIN_JS)
                    if os.path.isfile(p):
                        return Path(p)
    except Exception:
        pass
    raise RuntimeError("找不到 AutoClaw 客户端主程序（main.cjs）。可设环境变量 AUTOCLAW_BUNDLE 指向它。")


# ---------------------------------------------------------------- A 路：2.0.1
def extract_by_const(src: str):
    """找 ZWORK_*_SYSTEM_PROMPT 之类的常量声明，交给 node 求值。"""
    names = re.findall(r"\b(ZWORK_[A-Z0-9_]*SYSTEM_PROMPT[A-Z0-9_]*)\b", src)
    names = list(dict.fromkeys(names))
    if not names:
        return None, "A 路：bundle 里没有 ZWORK_*_SYSTEM_PROMPT"

    # 找到每个名字的声明语句，按出现顺序切片（拓扑序已由 bundle 顺序保证）
    decls = []
    for n in names:
        m = re.search(r"(?:var|let|const)\s+" + re.escape(n) + r"\s*=", src)
        if not m:
            continue
        end = _stmt_end(src, m.start())
        decls.append(src[m.start():end])
    if not decls:
        return None, "A 路：找到了名字但取不到声明"

    code = "\n".join(decls) + "\n;__R__=" + names[0] + ";"
    out = _eval_js(code)
    if out and len(out) > 200:
        return out, "A 路（ZWORK_*_SYSTEM_PROMPT）"
    return None, "A 路：求值失败"


# ---------------------------------------------------------------- B 路：2.2.0
def _qen_parts(src: str):
    """
    精确解析 Qen() 的返回数组，返回元素列表（字符串字面量给文本，标识符给名字）。
      function Qen(n=ybe,e=!0){return[`You are ${n}. ...`,...e?[Xen]:[],Fni,ven,wen].join(" ")}
    解析出：["(模板)", "Xen", "Fni", "ven", "wen"]
    """
    i = src.find("function Qen(")
    if i < 0:
        return None
    # 参数表里的形参名（这些是局部变量，不是要抓的常量）
    pl = src.index("(", i) + 1
    pr = src.index(")", pl)
    params = set()
    for seg in src[pl:pr].split(","):
        seg = seg.strip()
        if seg:
            params.add(seg.split("=")[0].strip())
    # 函数体
    j = src.index("{", pr)
    depth = 0
    k = j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    body = src[j:k + 1]
    # 返回语句的数组字面量
    rb = body.index("[")
    depth = 0
    re_ = rb
    while re_ < len(body):
        if body[re_] == "[":
            depth += 1
        elif body[re_] == "]":
            depth -= 1
            if depth == 0:
                break
        re_ += 1
    arr = body[rb + 1:re_]

    parts = []
    pos = 0
    while pos < len(arr):
        c = arr[pos]
        if c in " \t\n\r,":
            pos += 1
            continue
        if c == "`":                       # 模板字面量 -> 保留原文，稍后替换 ${n}
            e = pos + 1
            esc = False
            while e < len(arr):
                if esc:
                    esc = False
                elif arr[e] == "\\":
                    esc = True
                elif arr[e] == "`":
                    break
                e += 1
            parts.append(("text", arr[pos + 1:e]))
            pos = e + 1
        elif arr.startswith("...", pos):   # 展开：...e?[Xen]:[]  -> 抓 Xen
            e = arr.index("[", pos)
            close = arr.index("]", e)
            ident = arr[e + 1:close].strip()
            if ident:
                parts.append(("ident", ident))
            pos = close + 1
        elif c.isalpha() or c == "_":      # 裸标识符
            e = pos
            while e < len(arr) and (arr[e].isalnum() or arr[e] == "_"):
                e += 1
            name = arr[pos:e]
            if name not in params:
                parts.append(("ident", name))
            pos = e
        else:
            pos += 1
    return parts, params


def extract_by_qen(src: str):
    """
    把 Qen 函数本体 + 它引用的常量声明原样丢给 node 求值。

    为什么不让 Python 复刻拼接语义：Qen 里 Fni/ven/wen 是**数组**，
    JS 的 `[a, arr, arr].join(" ")` 对嵌套数组走 String() -> 逗号拼接，
    手写很容易写成空格拼接，结果就差一个字符。让 JS 自己跑最准。
    """
    try:
        parts, params = _qen_parts(src)
    except Exception as e:
        return None, f"B 路：解析 Qen 失败 {e}"
    if not parts:
        return None, "B 路：Qen 返回数组解析为空"

    # 1) Qen 函数本体（原样，含默认参数）
    fi = src.find("function Qen(")
    if fi < 0:
        return None, "B 路：找不到 Qen"
    pr = src.index(")", src.index("(", fi))
    j = src.index("{", pr)
    depth = 0
    k = j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    qen_src = src[fi:k + 1]

    # 2) 形参默认值引用的变量（产品名）也要声明
    needed = set()
    m = re.search(r"function\s+Qen\s*\(([^)]*)\)", qen_src)
    if m:
        for seg in m.group(1).split(","):
            if "=" in seg:
                needed.add(seg.split("=", 1)[1].strip())
    for kind, val in parts:
        if kind == "ident":
            needed.add(val)

    decls = []
    for name in sorted(needed):
        if not IDENT_RE.fullmatch(name or ""):
            continue
        d = _literal_decl(src, name)
        if d:
            decls.append(d)
    if not decls:
        return None, "B 路：取不到任何常量声明"

    code = "\n".join(decls) + "\n" + qen_src + "\n;__R__=Qen();"
    out = _eval_js(code)
    if out and len(out) > 200:
        return out, "B 路（Qen 原样求值，2.2.0）"
    return None, "B 路：求值失败"


def _literal_decl(src: str, name: str):
    """
    找 name 的声明，且右值必须是字符串/数组字面量（防止同名变量在别处的无关声明）。
    兼容两种写法：`var NAME = ...` 与压缩产物里的裸赋值 `NAME = ...`。
    返回整条语句文本；找不到返回 None。
    """
    pats = [
        r"(?:var|let|const)\s+" + re.escape(name) + r"\s*=\s*",   # 声明式
        r"(?<![\w$.])" + re.escape(name) + r"\s*=\s*",            # 裸赋值（排除 obj.name= 与 === ）
    ]
    for pat in pats:
        for m in re.finditer(pat, src):
            start = m.start()
            rhs = src[m.end():m.end() + 1]
            if rhs not in ("[", "\"", "'", "`"):
                continue
            if src[m.end() - 1:m.end()] == "=" and src[m.end():m.end() + 1] == "=":
                continue
            end = _stmt_end(src, start)
            return src[start:end]
    return None


# ---------------------------------------------------------------- 工具
def _stmt_end(s: str, i: int) -> int:
    """从声明起点做括号/引号配对，返回语句结束位置。"""
    j = s.index("=", i)
    k = j + 1
    depth = 0
    in_s = None
    esc = False
    while k < len(s):
        c = s[k]
        if in_s:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == in_s:
                in_s = None
        else:
            if c in "\"'`":
                in_s = c
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth == 0:
                    k += 1
                    while k < len(s) and s[k] in " \t\n":
                        k += 1
                    return k + 1 if k < len(s) and s[k] == ";" else k
            elif depth == 0 and c == ";":
                return k + 1
        k += 1
    return k


def _eval_js(code: str):
    """交给本机 node 求值，返回 __R__ 的值（字符串）或 None。"""
    node = _find_node()
    if not node:
        return None
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "t.js"
        f.write_text(code + "\n;process.stdout.write(String(__R__ ?? ''))", encoding="utf-8")
        try:
            r = subprocess.run([node, str(f)], capture_output=True, text=True,
                               timeout=30, encoding="utf-8", errors="replace")
        except Exception:
            return None
        if r.returncode != 0:
            return None
        return (r.stdout or "").strip() or None


def _find_node():
    for c in (os.environ.get("NODE_EXE"), "node"):
        if not c:
            continue
        try:
            r = subprocess.run([c, "-v"], capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    for p in (r"C:\Program Files\nodejs\node.exe",
              os.path.expandvars(r"%USERPROFILE%\.workbuddy\binaries\node\versions\22.22.2-6\node.exe")):
        if os.path.isfile(p):
            return p
    return None


# ---------------------------------------------------------------- 对外
def extract() -> tuple:
    """返回 (persona 文本 or None, 说明)。"""
    try:
        bundle = locate_client()
    except RuntimeError as e:
        return None, str(e)
    if bundle.suffix.lower() == ".asar":
        return None, (f"客户端是打包态（{bundle}），读不到 main.cjs。"
                      f"把 AUTOCLAW_BUNDLE 指向解包后的 main.cjs 再试。")
    src = bundle.read_text(encoding="utf-8", errors="replace")

    for fn in (extract_by_const, extract_by_qen):
        text, how = fn(src)
        if text:
            return text, how + f"  <- {bundle}"
    return None, "两条路都失败（客户端版本可能又改了拼装结构）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    out = Path(a.out)
    if a.check:
        if not out.is_file():
            print(json.dumps({"ok": False, "code": 1, "error": "persona.txt 不存在"}))
            return 1
        text, how = extract()
        same = bool(text) and out.read_text(encoding="utf-8").strip() == text.strip()
        print(json.dumps({"ok": same, "code": 0 if same else 3, "source": how,
                          "len": len(text or "")}, ensure_ascii=False))
        return 0 if same else 3

    text, how = extract()
    if not text:
        print(json.dumps({"ok": False, "code": 1, "error": how}, ensure_ascii=False))
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, out)
    print(json.dumps({"ok": True, "code": 0, "source": how, "len": len(text),
                      "head": text[:80]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
