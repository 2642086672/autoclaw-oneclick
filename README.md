# AutoClaw OneClick

**双击一下，把 AutoClaw 的模型额度变成你自己的本地 AI 接口。**

不需要懂命令行、不需要看长教程。双击 `图形界面.cmd`，浏览器里填几个路径，点「开始部署」，完事。

```
你的 AI 工具  →  http://127.0.0.1:18766/v1  →  AutoClaw 的模型额度
```

任何支持 OpenAI 或 Claude 格式的工具都能连上来用：Cherry Studio、ChatBox、Cline、
Claude Code、自己写的脚本……全部消耗 AutoClaw 账号里的额度。

---

## 快速开始

### 0. 前置（装一次就够）

| 要什么 | 怎么判断有没有 | 没有就去装 |
|---|---|---|
| Windows 10/11 | 你在用就是 | — |
| [Node.js](https://nodejs.org) 20+ | 命令行敲 `node -v` 有版本号 | 官网下 LTS |
| [Python](https://python.org) 3.10+ | 敲 `python --version` 有版本号 | 官网下，**勾选 Add to PATH** |
| AutoClaw 登录过 | 打开它能用 | 官网下载，手机号登录 |

### 1. 下载本仓库

绿色按钮 **Code → Download ZIP**，解压到任意目录。

### 2. 双击 `图形界面.cmd`

浏览器自动打开设置页。**所有路径都自动探测好了**，一般不用改：

| 字段 | 说明 |
|---|---|
| AutoClaw 安装位置 | 自动探测，猜错就手动改成 `...\AutoClaw2\resources\app\dist\main.cjs` |
| Node.js / Python | 自动探测 |
| API Key | 留空 = 自动生成一把新的 |
| 装开机自启 | 默认勾上（服务挂了每 5 分钟自动拉起） |

### 3. 点「开始部署」

下面日志区实时滚动，全部打勾后页面会给出：

```
OpenAI   baseUrl : http://127.0.0.1:18766/v1
Anthropic baseUrl: http://127.0.0.1:18766
API Key          : sk-ac-xxxxxxxxxxxx
```

### 4. 填进你的 AI 工具

以 Cherry Studio / ChatBox 为例：模型服务商选 **OpenAI 兼容**，把上面三行抄进去，
模型选 `glm-5.3-flash`，保存，发条消息试试。

Claude 系工具（Cline / Claude Code）用 Anthropic 那行（末尾**不带** `/v1`）。

---

## 可用模型

| 模型 | 适合 |
|---|---|
| `glm-5.3-flash` | **日常首选**，快 |
| `glm-5.3` | 复杂任务，更强 |
| `auto` | 让它自己挑 |
| `auto-fast` | 挑个快的 |
| `deepseek-v4.1-flash` / `deepseek-v4-pro` | DeepSeek 系 |

---

## 不想用图形界面？

仓库里备了三种姿势，随便挑：

| 入口 | 风格 |
|---|---|
| `图形界面.cmd` | 浏览器表单，点按钮（推荐） |
| `一键部署.cmd` | 双击全自动跑完，屏幕打连接信息 |
| `管理.cmd` | 命令行数字菜单（启动/停止/换Key/刷凭证） |

命令行直接跑也行：

```bash
python webui.py --port 9090     # 图形界面
python oneclick.py --auto       # 一键部署
python oneclick.py              # 数字菜单
```

---

## 常见问题

<details>
<summary><b>双击没反应 / 一闪就没</b></summary>

Python 没装，或装的时候没勾 "Add python.exe to PATH"。重装勾上，或者用命令行
`python oneclick.py --auto` 跑，能直接看到报什么错。
</details>

<details>
<summary><b>浏览器没自动打开</b></summary>

双击时那个黑窗口上写着地址（默认 `http://127.0.0.1:9090/`），手动复制到浏览器。
端口被占就换：`python webui.py --port 9091`。
</details>

<details>
<summary><b>提示「找不到 AutoClaw 客户端」</b></summary>

AutoClaw 装的位置太偏，自动探测猜不到。右键 AutoClaw 桌面图标 → 打开文件所在位置，
往上找到有 `resources` 文件夹的那一层，填：

```
<那一层>\resources\app\dist\main.cjs
```

填在界面的「AutoClaw 安装位置」里。
</details>

<details>
<summary><b>AI 工具报 401 Invalid token</b></summary>

账号凭证过期了（AutoClaw 约一天轮换一次）。两个办法：

- 界面点「**刷新凭证**」（前提：AutoClaw 登录着）
- 或重新打开 AutoClaw 登录一次，再点部署

装了开机自启的话，守护脚本会自动跟着续期，一般遇不到这条。
</details>

<details>
<summary><b>AI 工具报 406</b></summary>

persona 没对上（这是 AutoClaw 网关的请求指纹校验）。重新跑一遍部署，让它重新抽。
注意 persona 必须原样使用，别手改。
</details>

<details>
<summary><b>AI 工具报 402</b></summary>

这个模型的额度用完了。等每日赠送，或换个模型（各模型额度分开算）。
</details>

<details>
<summary><b>AI 工具报 810001「系统繁忙」</b></summary>

`glm-5.3-flash` 白天高峰限流，夜里 23:00–9:00 畅通。白天换 `glm-5.3` 或 `auto`。
</details>

<details>
<summary><b>想换 API Key</b></summary>

界面点「生成新的」，然后**必须再点一次「开始部署」**才生效
（跑着的反代还在用旧 Key）。
</details>

<details>
<summary><b>想彻底卸载</b></summary>

界面/菜单里「移除开机自启」，然后删两个东西：

- 本工具的文件夹
- `C:\Users\<你>\.autoclaw-relay\`（运行目录）
</details>

---

## 它是怎么工作的

```
┌─────────────┐     OpenAI/Claude 格式      ┌──────────────────┐
│  你的 AI 工具 │ ──────────────────────────→ │  本地反代 127.0.0.1 │
└─────────────┘                              │      :18766       │
                                             └────────┬─────────┘
                                                      │ AutoClaw 官方渠道契约
                                                      │ （persona + 签名头 + 会话头）
                                                      ▼
                                             ┌──────────────────┐
                                             │  AutoClaw 云端网关  │
                                             └──────────────────┘
```

部署时自动做的六件事：

1. **检查环境** —— Node / Python / 依赖，缺啥提示啥
2. **取凭证** —— 从 AutoClaw 的加密存储解密出账号 token（DPAPI，仅本机可解）
3. **抽 persona** —— 从 AutoClaw 客户端程序里抽出网关要求的 system prompt（这是 406 闸门的关键）
4. **部署反代** —— 一个本地 Node 服务，把 OpenAI/Claude 格式翻译成 AutoClaw 云端格式
5. **装守护** —— 开机自启 + 每 5 分钟保活，凭证轮换自动跟随
6. **启动** —— 服务跑在 `127.0.0.1`，只监听本机

所有凭证、密钥都只存在你自己电脑上，不经过任何第三方服务器。

---

## 目录结构

```
autoclaw-oneclick/
├── 图形界面.cmd          ← 双击这个（浏览器表单）
├── 一键部署.cmd          ← 或双击这个（全自动无界面）
├── 管理.cmd              ← 或这个（命令行菜单）
├── webui.py              图形界面服务（纯 Python 标准库，零第三方依赖）
├── oneclick.py           命令行主程序
├── assets/
│   ├── server_2x.mjs        反代核心
│   ├── persona_extract.py   persona 抽取（兼容 2.0.x / 2.2.x 两代客户端）
│   ├── make_compat_auth.py  凭证解密
│   └── a_switch.py          凭证解密底层实现
└── LICENSE
```

运行时生成（不在仓库里）：`~/.autoclaw-relay/`（反代程序、persona、日志、你的 Key）。

---

## 致谢与声明

- 反代核心来自 [autoclaw-switch](https://github.com/apmengzi/autoclaw-switch)
  （MIT）—— 406 闸门的五要素逆向结论全部出自该项目，站在它肩膀上。
- AutoClaw 是智谱 / Z.ai 的产品，本项目与其无关。
- 本项目通过逆向实现非官方接口调用，仅供学习研究；账号封禁、额度损失由使用者自行承担。
