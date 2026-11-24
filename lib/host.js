// DSH「链接微信」· 宿主半边
//
// 做四件事：
//   1. 微信 → DSH：Python 桥接把新消息 POST 到 /dsh-wechat-bridge/receive，
//      我们把它喂进绑定好的那间会话（agent.followup），让它照常开一轮。
//   2. DSH → 微信：伙伴说完一整轮，把最后一句压进队列，
//      Python 桥接每两秒来 /outbox 取一次，用 UI 自动化发出去。
//   3. 管 Python 那半的命：起停进程、收日志（Python 和依赖由使用者自己装好）
//   4. 什么也不碰微信协议 —— 收是读本地库，发是驱动窗口，都在 Python 那半。
//
// ⚠ 安全默认：总开关 = 关、试运行 = 开 —— 不手动打开，一句都不会真的发到微信。

import { readFileSync, writeFileSync, mkdirSync, existsSync, appendFileSync, statSync, renameSync, unlinkSync, readdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { spawn, execFile } from 'node:child_process'
import { 描述图片们 } from './vision.js'

export const name = 'wechat-bridge'

/** 宿主得给：webServer（路由 + 自己的地址）、agents（followup）、sessions / sessionTitle（挑会话）。 */
export const inject = ['agents', 'sessions', 'sessionTitle', 'webServer', 'workspaceRegistry', 'timer', 'tools']

// ---------- 常量 ----------

const 插件根 = dirname(dirname(fileURLToPath(import.meta.url)))
const 桥脚本 = join(插件根, 'bridge', 'wechat_bridge.py')
const 目录 = process.env.DSH_HOME || join(homedir(), '.dsh')
const 配置文件 = join(目录, 'wechat-bridge-config.json')
const 路由前缀 = '/dsh-wechat-bridge'
/** 插件版本：从 package.json 读，免得跟写死的那份对不上。 */
const 插件版本 = (() => {
  try {
    // 用记事本之类的存过会带 BOM，JSON.parse 见到 BOM 直接抛错，先剥掉
    const 原文 = String(readFileSync(join(插件根, 'package.json'), 'utf8')).replace(/^\uFEFF/, '')
    return String(JSON.parse(原文).version || '0.0.0')
  } catch (error) {
    return '0.0.0'
  }
})()
const 日志上限 = 80
/** 本项目的关键改动标记。
 *  标记只在装了本项目的版本里才有；改了实现就顺手改这儿。 */
const 补丁标记 = [
  ['lib/host.js', ['活跃群: 串们(', '主动开口一次', '掉线: 掉线', 'wechat_send']],
  ['lib/client.js', ['主动对话', '活跃群']],
  ['bridge/wechat_bridge.py', ['def 拟人延迟秒()', 'def _清空剪贴板():', 'def _剪贴板设字(', 'def _报微信掉线(', '_是我发的(']],
]

/** 返回缺了什么（空数组 = 补丁齐）。 */
function 查补丁缺失() {
  const 缺 = []
  for (const [相对, 标记们] of 补丁标记) {
    let 原文 = ''
    try {
      原文 = String(readFileSync(join(插件根, 相对), 'utf8'))
    } catch (error) {
      缺.push(`${相对}（读不到）`)
      continue
    }
    const 少 = 标记们.filter((m) => !原文.includes(m))
    if (少.length) 缺.push(`${相对} 缺 ${少.join(' / ')}`)
  }
  return 缺
}
const 进程日志上限 = 200
/** 被微信叫醒之后，多久以内这一轮才算「该发回微信」的。 */
const 唤醒窗口 = 10 * 60 * 1000

const 默认配置 = {
  总开关: false,
  试运行: true,
  默认会话: '',
  绑定: [],
  令牌: '',
  解释器: '',
  /** 看图：微信里发来的图片 / 表情包，先让视觉模型看一遍 */
  看图: true,
  视觉密钥: '',
  视觉模型: 'deepseek-flash',
  视觉地址: 'https://api.deepseek.com',
  视觉备用密钥: '',
  视觉备用模型: 'glm-4v-flash',
  视觉备用地址: 'https://open.bigmodel.cn/api/paas/v4',
  /** 群消息进 DSH 时，往前带几条上下文（0 = 不带） */
  群上下文条数: 6,
  /** 把群聊记成日记（按天一个文件） */
  日记: true,
  日记目录: '',
  /** 这些 wxid 算主人：只认 id，不认名字（名字谁都能改） */
  主人: [],
  /** 主人的微信显示名 —— 主动搭话要发私聊时用它 */
  主人显示名: '',
  /** 主动对话：没人找它的时候，自己按点找主人说句话 */
  主动对话: false,
  /** 隔多久主动开口一次（分钟）；也是「闲多久才算空下来」 */
  主动间隔分钟: 90,
  /** 主动对话的时段（0–24，含起不含止），半夜别吵 */
  主动时段起: 8,
  主动时段止: 23,
  /** 主动开口时塞给它的提示词，留空用默认 */
  主动提示: '',
  /** 拟人延迟：真发之前随机等这么多秒（下限 / 上限；都填 0 = 不等，秒回） */
  回复延迟下限秒: 8,
  回复延迟上限秒: 25,
  /** 这些名字（会话名或发送者昵称）不回复、也不读 */
  屏蔽: ['微信运动', '微信团队', '微信支付', '服务通知', '订阅号消息', '折叠的群聊'],
  /** 同一个群两条回复之间至少隔几秒（0 = 不限） */
  群回复间隔秒: 30,
  /** 同一个群每小时最多接几次（0 = 不限） */
  群每小时上限: 6,
  /** 活跃群：这几个群里，没点名的群友闲聊也会进 DSH（空 = 只有点名 / 主人说话才进） */
  活跃群: [],
  /** 只有主人能让它动电脑；外人只能聊天（工具会被硬拦） */
  外人只能聊天: true,
  /** 入站白名单：空 = 谁都能发进来；填了只有名单里的人能触发一轮（主人永远放行） */
  入站白名单: [],
  /** 用量折算成钱：元 / 百万 tokens（默认按 DeepSeek 官方价，设置里能改） */
  价_缓存读: 0.025,
  价_输入: 3,
  价_输出: 6,
}

// ---------- 小工具 ----------

const 空轮 = () => ({ 轮号: -1, 文本: '', 图: [], 来源: '' })

const 掐短 = (s, n = 60) => {
  const t = String(s == null ? '' : s).replace(/\s+/g, ' ').trim()
  return t.length > n ? t.slice(0, n) + '…' : t
}

const 会话键 = (id) => String(id == null ? '' : id).trim().replace(/^session-/, '')

const 环形 = (arr, 上限) => {
  if (arr.length > 上限) arr.splice(0, arr.length - 上限)
  return arr
}

const 时间戳 = () => new Date().toLocaleTimeString('zh-CN', { hour12: false })

const 串们 = (v) => {
  if (Array.isArray(v)) return v.map((x) => String(x).trim()).filter(Boolean)
  return String(v == null ? '' : v)
    .split(/[,，\n]/)
    .map((x) => x.trim())
    .filter(Boolean)
}

export function apply(ctx) {
  const 日志 = []
  // 桥接最近一次「出事了，要人管」的汇报（目前只有微信掉线）。只展示，不自动处理。
  let 掉线 = null
  const 记账 = (t) => {
    日志.push(`[${时间戳()}] ${String(t)}`)
    环形(日志, 日志上限)
    try {
      ctx.logger?.info(`wechat-bridge: ${t}`)
    } catch (error) {}
    try {
      appendFileSync(join(目录, 'wechat-bridge-host.log'), `[${时间戳()}] ${String(t)}\n`)
      滚宿主日志()
    } catch (error) {}
  }

  /** 宿主日志只增不减会涨到几十 MB；超过 4MB 滚成 .log.1（只留一代）。 */
  let 日志滚到 = 0
  function 滚宿主日志() {
    const now = Date.now()
    if (now - 日志滚到 < 60000) return
    日志滚到 = now
    try {
      const p = join(目录, 'wechat-bridge-host.log')
      if (!existsSync(p) || statSync(p).size < 4 * 1024 * 1024) return
      const 旧 = `${p}.1`
      if (existsSync(旧)) unlinkSync(旧)
      renameSync(p, 旧)
    } catch (error) {}
  }

  /** 补丁自检。升级 / 重装会把 node_modules 里的改动冲掉，
   *  每次插件加载对一遍关键标记；缺了就写进宿主日志，别等哪天发现她不发图了才回头查。 */
  function 自检补丁() {
    try {
      const 缺 = 查补丁缺失()
      if (!缺.length) return
      记账(`⚠ 本项目的改动缺失（升级或重装把文件覆盖回了旧版本）：${缺.join('；')}`)
      记账('补救：用本仓库的文件覆盖安装目录')
    } catch (error) {}
  }

  // ---------- 配置 ----------
  /** 价格这类数字：非法 / 负数就回默认 */
  const 数 = (v, 默认) => {
    const n = Number(v)
    return Number.isFinite(n) && n >= 0 ? n : 默认
  }

  const 归一化 = (o) => {
    const 原 = o && typeof o === 'object' ? o : {}
    const 绑定 = Array.isArray(原.绑定)
      ? 原.绑定
          .filter((x) => x && x.聊天 && x.会话)
          .map((x) => ({ 聊天: String(x.聊天).trim(), 会话: String(x.会话).trim() }))
      : []
    const 按聊天 = new Map()
    for (const b of 绑定) 按聊天.set(b.聊天, b)
    return {
      总开关: 原.总开关 === true,
      试运行: 原.试运行 !== false,
      默认会话: String(原.默认会话 || '').trim(),
      绑定: [...按聊天.values()],
      令牌: String(原.令牌 || ''),
      解释器: String(原.解释器 || '').trim(),
      看图: 原.看图 !== false,
      视觉密钥: String(原.视觉密钥 || '').trim(),
      视觉模型: String(原.视觉模型 || 'deepseek-flash').trim() || 'deepseek-flash',
      视觉地址: String(原.视觉地址 || 'https://api.deepseek.com').trim().replace(/\/+$/, ''),
      视觉备用密钥: String(原.视觉备用密钥 || '').trim(),
      视觉备用模型: String(原.视觉备用模型 || 'glm-4v-flash').trim() || 'glm-4v-flash',
      视觉备用地址: String(原.视觉备用地址 || 'https://open.bigmodel.cn/api/paas/v4')
        .trim()
        .replace(/\/+$/, ''),
      群上下文条数:
        原.群上下文条数 == null ? 6 : Math.max(0, Math.min(50, Number(原.群上下文条数) || 0)),
      日记: 原.日记 !== false,
      日记目录: String(原.日记目录 || '').trim(),
      主人: 串们(原.主人),
      主人显示名: String(原.主人显示名 || '').trim(),
      主动对话: 原.主动对话 === true,
      主动间隔分钟: 原.主动间隔分钟 == null ? 90 : Math.max(5, Number(原.主动间隔分钟) || 90),
      主动时段起: 原.主动时段起 == null ? 8 : Math.max(0, Math.min(23, Number(原.主动时段起) || 0)),
      主动时段止: 原.主动时段止 == null ? 23 : Math.max(1, Math.min(24, Number(原.主动时段止) || 24)),
      主动提示: String(原.主动提示 || ''),
      回复延迟下限秒: 原.回复延迟下限秒 == null ? 8 : Math.max(0, Math.min(300, Number(原.回复延迟下限秒) || 0)),
      回复延迟上限秒: 原.回复延迟上限秒 == null ? 25 : Math.max(0, Math.min(600, Number(原.回复延迟上限秒) || 0)),
      屏蔽: 原.屏蔽 == null ? [...默认配置.屏蔽] : 串们(原.屏蔽),
      群回复间隔秒: 原.群回复间隔秒 == null ? 30 : Math.max(0, Number(原.群回复间隔秒) || 0),
      群每小时上限: 原.群每小时上限 == null ? 6 : Math.max(0, Number(原.群每小时上限) || 0),
      活跃群: 串们(原.活跃群),
      外人只能聊天: 原.外人只能聊天 !== false,
      入站白名单: 串们(原.入站白名单),
      价_缓存读: 数(原.价_缓存读, 默认配置.价_缓存读),
      价_输入: 数(原.价_输入, 默认配置.价_输入),
      价_输出: 数(原.价_输出, 默认配置.价_输出),
    }
  }

  const 读配置 = () => {
    try {
      return 归一化(JSON.parse(readFileSync(配置文件, 'utf8').replace(/^\uFEFF/, '')))
    } catch (error) {
      return 归一化(默认配置)
    }
  }

  const 写配置 = (c) => {
    try {
      mkdirSync(目录, { recursive: true })
      writeFileSync(配置文件, JSON.stringify(c, null, 2), 'utf8')
      return true
    } catch (error) {
      记账(`配置写不进去：${String((error && error.message) || error)}`)
      return false
    }
  }

  // ---------- 用量账本：微信里谁花掉了多少 token ----------

  const 用量文件 = join(目录, 'wechat-bridge-usage.json')

  const 读用量 = () => {
    try {
      const o = JSON.parse(readFileSync(用量文件, 'utf8').replace(/^\uFEFF/, ''))
      if (o && typeof o === 'object' && o.users && typeof o.users === 'object') return o
    } catch (error) {}
    return { users: {} }
  }

  const 写用量 = (u) => {
    try {
      mkdirSync(目录, { recursive: true })
      writeFileSync(用量文件, JSON.stringify(u, null, 2), 'utf8')
      return true
    } catch (error) {
      return false
    }
  }

  const 今天键 = () => {
    const d = new Date()
    const p = (n) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
  }

  const 空格桶 = (u) => ({
    input: Number(u && u.input) || 0,
    output: Number(u && u.output) || 0,
    cacheRead: Number(u && u.cacheRead) || 0,
    turns: Number(u && u.turns) || 0,
  })

  const 加桶 = (a, b) => ({
    input: a.input + b.input,
    output: a.output + b.output,
    cacheRead: a.cacheRead + b.cacheRead,
    turns: a.turns + b.turns,
  })

  /** 把一条 usage 记到某人头上 */
  const 记用量 = (谁id, 谁名, usage) => {
    if (!usage) return
    const 本 = {
      input: Number(usage.inputTokens) || 0,
      output: Number(usage.outputTokens) || 0,
      cacheRead: Number(usage.cacheReadTokens) || 0,
      turns: 1,
    }
    if (!本.input && !本.output && !本.cacheRead) return
    const 账 = 读用量()
    const 人键 = String(谁id || 谁名 || 'local')
    const 人 = 账.users[人键] || { name: 谁名 || '本地', input: 0, output: 0, cacheRead: 0, turns: 0, days: {} }
    人.name = 谁名 || 人.name
    const 总 = 加桶(空格桶(人), 本)
    人.input = 总.input
    人.output = 总.output
    人.cacheRead = 总.cacheRead
    人.turns = 总.turns
    人.last = Date.now()
    人.days = 人.days && typeof 人.days === 'object' ? 人.days : {}
    const 日键 = 今天键()
    人.days[日键] = 加桶(空格桶(人.days[日键]), 本)
    const 日们 = Object.keys(人.days).sort()
    while (日们.length > 60) delete 人.days[日们.shift()]
    账.users[人键] = 人
    写用量(账)
  }

  const 算钱 = (配置, u) => {
    const 百万 = 1e6
    return (
      (u.input / 百万) * 配置.价_输入 +
      (u.output / 百万) * 配置.价_输出 +
      (u.cacheRead / 百万) * 配置.价_缓存读
    )
  }

  const 多少钱 = (元) => {
    if (!元) return '¥0'
    return 元 < 0.01 ? '¥' + 元.toFixed(4) : '¥' + 元.toFixed(2)
  }

  const 多少数 = (n) => {
    if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿'
    if (n >= 1e4) return (n / 1e4).toFixed(2) + ' 万'
    return String(n)
  }

  /** 谁在问就回谁的账；主人问的时候额外带一行总计 */
  const 用量报告 = (配置, 谁id, 谁名, 全体) => {
    const 账 = 读用量()
    const 人 = 账.users[String(谁id || '')] || 账.users[String(谁名 || '')] || null
    const 行 = []
    if (人) {
      const 总 = 空格桶(人)
      const 日 = 空格桶(人.days && 人.days[今天键()])
      const 名 = 人.name || 谁名 || '你'
      行.push(`${名}：累计 ${总.turns} 轮`)
      行.push(`输入 ${多少数(总.input)} / 输出 ${多少数(总.output)} / 缓存读 ${多少数(总.cacheRead)}`)
      行.push(`约 ${多少钱(算钱(配置, 总))}`)
      if (日.turns) 行.push(`今天 ${日.turns} 轮，约 ${多少钱(算钱(配置, 日))}`)
    } else {
      行.push(`${谁名 || '你'}：还没有记录`)
    }
    if (全体) {
      let 合 = { input: 0, output: 0, cacheRead: 0, turns: 0 }
      for (const k of Object.keys(账.users)) 合 = 加桶(合, 空格桶(账.users[k]))
      行.push(`全部 ${合.turns} 轮，约 ${多少钱(算钱(配置, 合))}`)
    }
    return 行.join('\n')
  }

  // ---------- 会话 ----------

  const 会话清单 = () => {
    const 出 = []
    try {
      const 归档 = new Set()
      try {
        const 登记 = ctx.workspaceRegistry || ctx.get('workspaceRegistry')
        for (const x of (登记 && 登记.archivedSessionIds) || []) 归档.add(String(x))
      } catch (error) {}
      const 会话服务 = ctx.get('sessions')
      const 标题服务 = ctx.get('sessionTitle')
      if (会话服务 && typeof 会话服务.list === 'function') {
        for (const s of 会话服务.list()) {
          if (归档.has(String(s.id))) continue
          let 标题 = ''
          try {
            const 快照 = 标题服务 && typeof 标题服务.get === 'function' ? 标题服务.get(s) : undefined
            标题 = 快照 && 快照.title ? String(快照.title) : ''
          } catch (error) {}
          出.push({ id: String(s.id), 标题 })
        }
      }
    } catch (error) {}
    return 出
  }

  const 找agent = (id) => {
    const 原 = String(id == null ? '' : id).trim()
    if (原 === '') return undefined
    const 候选 = [原]
    if (原.startsWith('session-')) 候选.push(原.slice('session-'.length))
    else 候选.push('session-' + 原)
    for (const 一个 of 候选) {
      try {
        const a = ctx.agents.get(一个)
        if (a !== undefined) return a
      } catch (error) {}
    }
    return undefined
  }

  const 新id = () => {
    try {
      return randomUUID()
    } catch (error) {
      return 'wx-' + Date.now() + '-' + Math.random().toString(36).slice(2)
    }
  }

  const 提取正文 = (message) => {
    if (!message) return ''
    const c = message.content
    if (typeof c === 'string') return c
    if (!Array.isArray(c)) return ''
    return c
      .filter((x) => x && x.type === 'text' && typeof x.text === 'string')
      .map((x) => x.text)
      .join('')
  }

  /** 从消息里抽本地图片路径（附件形式的图）。 */
  const 提取图 = (message) => {
    const out = []
    const c = message && message.content
    if (!Array.isArray(c)) return out
    for (const x of c) {
      if (!x || x.type !== 'image') continue
      const 候选 = [
        x.path,
        x.localPath,
        x.filePath,
        x.attachment && (x.attachment.hostPath || x.attachment.path || x.attachment.localPath),
      ]
      for (const p of 候选) {
        if (p && typeof p === 'string' && existsSync(p)) {
          out.push(p)
          break
        }
      }
    }
    return out
  }

  /** 正文里写的 markdown 图片也算（`![x](C:\a.png)` 或本机 DSH 服务的 `http://127.0.0.1:端口/...`），并从文字里摘掉，免得发出去一串路径。 */
  // 发微信之前把不像人话的东西洗掉：整段代码块、行内反引号、工具/系统标签。
// 2026-11-17 07:54：回复里带了一截 `<job id="1" status="running" kind…` 被原样发进微信。
const 洗掉代码和标签 = (s) => {
  let t = String(s || '')
  t = t.replace(/```[\s\S]*?```/g, '')
  t = t.replace(/`([^`\n]*)`/g, '$1')
  t = t.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/gi, '')
  t = t.replace(/<job\b[\s\S]*$/gi, '')
  t = t.replace(/<\/?(job|tool|function_calls?|analysis|commentary|instruction|system)[^>]*>/gi, '')
  t = t.replace(/<\/?[a-zA-Z][\w:-]*(?:\s[^>]*)?>/g, '')
  return t.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim()
}

const 摘markdown图 = (text) => {
    const 图 = []
    const 剩 = String(text || '').replace(/!\[[^\]]*\]\(([^)\s]+)\)/g, (m, p) => {
      let q = String(p)
      if (q.startsWith('file:///')) {
        try {
          q = decodeURIComponent(q.slice(8)).replace(/\//g, '\\')
        } catch (error) {
          // URL 里混了坏的 % 转义就按原文处理，不能让一条消息把整段提取弄崩
          q = q.slice(8).replace(/\//g, '\\')
        }
      }
      // 表情包插件给的是本机 HTTP 地址（127.0.0.1:端口/x-nekowhale-stickers/...），
      // 先记下来，发给微信前由 Python 那半下载成本地文件再发 —— 不然微信只会收到一行 markdown。
      if (/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?\//i.test(q)) {
        图.push(q)
        return ''
      }
      if (existsSync(q)) {
        图.push(q)
        return ''
      }
      return m
    })
    return { 文本: 剩.trim(), 图 }
  }

  // ---------- 出站队列（DSH → 微信）----------

  const outbox = []
  const 上一轮们 = new Map()
  /** 这一轮是哪个微信用户说的 —— 用量记到他头上 */
  const 当前说话人 = new Map()
  /** 这一轮是不是「主动搭话」自己起的头（跟微信无关，但话要说给主人听） */
  const 主动搭话轮 = new Map()
  const 已发轮 = new Set()
  /** 这一轮是「微信叫的」还是「本地自己聊的」——按轮记，别拿十分钟窗口糊弄 */
  const 轮来源 = new Map()
  const 唤醒时间 = new Map()
  const 回给表 = new Map()
  /** 这间绑定会话最后一次有动静的时间 —— 主动对话靠它判断「闲了多久」 */
  const 最后活动 = new Map()

  const 压队列 = (项) => {
    outbox.push(项)
    环形(outbox, 50)
  }

  const 聊天到会话 = (聊天, 聊天id) => {
    const 配置 = 读配置()
    const 名 = String(聊天 == null ? '' : 聊天).trim()
    const 码 = String(聊天id == null ? '' : 聊天id).trim()
    // 绑定的左边既可以填「微信里显示的名字」，也可以填 wxid / xxx@chatroom。
    // 名字会被改备注、群里还可能有同名，所以底层那个码永远认一份 —— 填哪个都能对上。
    const 命中 = (配置.绑定 || []).find((b) => b.聊天 === 名 || (码 && b.聊天 === 码))
    return (命中 && 命中.会话) || 配置.默认会话 || ''
  }

  // ---------- 收消息：微信 → DSH ----------

  // 外人轮：这一轮是微信里「非主人」叫来的 —— 工具一律不许动（不是靠嘴劝，是硬拦）
  const 外人轮 = new Map()
  const 挂了工具闸 = new Set()

  // 认人看「微信里显示的名字」（昵称/备注），wxid 也认一份当补充。
  // 规矩：微信里叫什么就是谁，别看底层那串英文码。
  const 是主人 = (谁id, 谁名) => {
    const 名单 = 读配置().主人 || []
    if (!名单.length) return false
    return 名单.some((m) => m && ((谁id && m === 谁id) || (谁名 && m === 谁名)))
  }

  /** 给这间会话的 agent 挂一个工具闸：外人轮里任何工具调用都被拒。 */
  const 挂工具闸 = (agent, 键) => {
    if (!agent || !键 || 挂了工具闸.has(键)) return
    try {
      const 工具 = agent.ctx && agent.ctx.tools
      if (!工具 || typeof 工具.guard !== 'function') {
        记账('这层宿主没有 tools.guard —— 外人只能聊天这条挡不住，自己留神')
        return
      }
      const off = 工具.guard(() => {
        if (外人轮.has(键)) {
          return '这一轮是从微信转进来的，说话的人不是你主人 —— 只能聊天，不能调用任何工具（不能跑命令、不能读写文件、不能改设置）。'
        }
        return undefined
      })
      挂了工具闸.add(键)
      ctx.effect(() => () => {
        try {
          if (typeof off === 'function') off()
        } catch (error) {}
        挂了工具闸.delete(键)
      }, `wechat-bridge: 工具闸 ${键}`)
    } catch (error) {
      记账('挂工具闸失败：' + String((error && error.message) || error))
    }
  }


  // ---------- 最近聊过谁（设置页的「绑定」和「屏蔽」直接从这里点，不用手打名字） ----------

  const 聊天文件 = join(目录, 'wechat-bridge-chats.json')
  let 聊天缓存 = null

  const 读聊天表 = () => {
    if (聊天缓存) return 聊天缓存
    let 行 = []
    try {
      const o = JSON.parse(readFileSync(聊天文件, 'utf8').replace(/^\uFEFF/, ''))
      if (Array.isArray(o)) 行 = o
      else if (o && Array.isArray(o.聊天)) 行 = o.聊天
    } catch (error) {
      行 = []
    }
    聊天缓存 = 行.filter((x) => x && String(x.名 || '').trim())
    return 聊天缓存
  }

  const 写聊天表 = (行) => {
    聊天缓存 = 行
    try {
      mkdirSync(目录, { recursive: true })
      writeFileSync(聊天文件, JSON.stringify(行, null, 2), 'utf8')
    } catch (error) {}
  }

  /**
   * 每来一条微信消息记一笔：名字、底层 id、群还是私聊、谁说的、什么时候。
   * 设置页据此给下拉候选；改了备注也不怕对不上。
   */
  const 记最近聊天 = (名, id, 类型, 谁) => {
    const 真名 = String(名 || '').trim()
    if (!真名 || 真名 === '微信') return
    const 状 = String(类型 || '').includes('群') ? '群' : '私聊'
    const 旧 = 读聊天表().filter((x) => String(x.名) !== 真名)
    旧.unshift({ 名: 真名, id: String(id || '').trim(), 类型: 状, 谁: String(谁 || ''), 最后: Date.now() })
    写聊天表(旧.slice(0, 60))
  }

  const 最近聊天列 = () =>
    读聊天表()
      .slice()
      .sort((a, b) => Number(b.最后 || 0) - Number(a.最后 || 0))
      .slice(0, 40)
      .map((x) => ({ 名: String(x.名), id: String(x.id || ''), 类型: String(x.类型 || '私聊'), 最后: Number(x.最后 || 0) }))

  // ---------- 好感度（只给「不是主人」的人用） ----------
  //
  // 外人不进记忆，只留一个数字挂在 wxid 上：每说一句 +0.2，够它下次开口时「认得这个人」。
  // 数据在 ~/.dsh/wechat-affinity.json，重启、换会话都还在。

  const 好感文件 = join(目录, 'wechat-affinity.json')

  function 记好感度(id, 名) {
    let 表 = {}
    try {
      if (existsSync(好感文件)) 表 = JSON.parse(readFileSync(好感文件, 'utf8')) || {}
    } catch (error) {
      表 = {}
    }
    const 键 = id || 名 || 'unknown'
    const 旧 = 表[键] || { name: 名 || '', score: 0, n: 0 }
    if (名) 旧.name = 名
    旧.n = (旧.n || 0) + 1
    旧.score = Math.round(((旧.score || 0) + 0.2) * 10) / 10
    旧.last = Date.now()
    表[键] = 旧
    try {
      writeFileSync(好感文件, JSON.stringify(表, null, 2), 'utf8')
    } catch (error) {}
    return 旧.score
  }

  async function 转入消息(来) {
    const 原文字 = String((来 && (来.text ?? 来.content)) || '').trim()
    const 图片们 = (Array.isArray(来 && 来.图片) ? 来.图片 : [])
      .map((x) => String(x == null ? '' : x).trim())
      .filter(Boolean)
      .slice(0, 3)
    const 聊天 = String((来 && (来.chat || 来.chatId)) || '').trim() || '微信'
    const 聊天id = String((来 && 来.chatId) || '').trim()
    const 谁 = String((来 && (来.sender || 来.nickname)) || '').trim()
    if (!原文字 && !图片们.length) return { ok: false, 说: '没有文字也没有图片（body 里给 text / 图片）' }

    const 配置 = 读配置()
    if (!配置.总开关) return { ok: false, 说: '总开关关着 —— 去设置里打开' }

    // 入站白名单：空 = 谁都能进（默认）；填了只放行名单里的（主人永远放行，不用写进来）
    const 白名单 = 配置.入站白名单 || []
    if (白名单.length) {
      const 来id = String((来 && (来.senderId || 来.sender_id)) || '').trim()
      const 来名 = String((来 && (来.sender || 来.nickname)) || '').trim()
      const 放行 =
        是主人(来id, 来名) || (来id && 白名单.includes(来id)) || (来名 && 白名单.includes(来名))
      if (!放行) {
        记账(`入站白名单拦下一条（${聊天} / ${来名 || 来id || '?'}）：没转进去`)
        return { ok: false, 说: '这个发送者不在入站白名单里 —— 没有转进 DSH' }
      }
    }

    // 「用量」这种问句不送去烧 token，直接本地查账（谁问回谁的）
    const 问账的 = 原文字 && /^(用量|花费|花了多少|花了多少钱|消耗|用了多少|token|tokens)\s*$/i.test(原文字)
    if (问账的) {
      const 谁id问 = String((来 && (来.senderId || 来.sender_id)) || '').trim()
      const 报 = 用量报告(配置, 谁id问, 谁, 是主人(谁id问, 谁))
      压队列({ id: 新id(), text: 报, images: [], chat: 聊天, at: Date.now() })
      记账(`回了一条用量（${聊天} / ${谁 || '?'}）`)
      return { ok: true, 说: '用量已经排进发件队列' }
    }

    const 是不是群 = String(来.chatType || 来.type || '').includes('group') || 聊天.endsWith('@chatroom')
    // 记一笔最近聊过谁：设置页「单独绑定 / 不回谁」直接从这份清单里挑，不用自己对着微信手打。
    记最近聊天(聊天, 聊天id, 是不是群 ? '群' : '私聊', 谁)

    const 目标 = 聊天到会话(聊天, 聊天id)
    if (!目标) return { ok: false, 说: '还没绑定会话 —— 设置里选一间「默认会话」' }

    let agent = 找agent(目标)
    if (!agent) {
      // 这间会话在磁盘上躺着，只是没被打开 —— 主动恢复一份，不然微信消息根本进不去
      try {
        await ctx.agents.resume({ resumeSessionId: 目标 })
        agent = 找agent(目标)
        if (agent) 记账(`会话 ${目标} 原本没开着，已经把它从磁盘唤起来了`)
      } catch (error) {
        记账(`自动唤醒会话失败（${目标}）：${String((error && error.message) || error)}`)
      }
    }
    if (!agent) return { ok: false, 说: `会话 ${目标} 现在没活着（先在 DSH 里打开它一次）` }

    const 谁id = String((来 && (来.senderId || 来.sender_id)) || '').trim()
    const 主人说的 = 是主人(谁id, 谁)
    const 署名 = 是不是群 && 谁 ? `${谁}：` : ''

    let 文字 = 原文字
    if (图片们.length) {
      const 类型 = String((来 && 来.图片类型) || '图片').includes('表情') ? '表情包' : '图片'
      let 描述 = ''
      if (配置.看图 !== false) {
        try {
          描述 = await 描述图片们(图片们, 类型, 配置, 记账)
        } catch (error) {
          记账(`看图出错：${String((error && error.message) || error)}`)
        }
      }
      const 标签 = 类型 === '表情包' ? '一个表情包' : '一张图片'
      const 前缀 = 描述
        ? `[对方发来${标签}：${描述}]`
        : `[对方发来${标签}${配置.看图 === false ? '' : '，没看清内容'}]`
      const 占位 = /（发来(?:一)?(?:张|个)?(?:图片|表情包)）/
      if (文字 && 占位.test(文字)) 文字 = 文字.replace(占位, () => 前缀)
      else if (文字) 文字 = `${前缀}\n${文字}`
      else 文字 = 前缀
    }

    let 文案 = `${署名}${文字}`
    if (!主人说的) {
      const 分 = 记好感度(谁id, 谁)
      文案 = `（这条是「${谁 || '微信里的人'}」发的，不是你主人。你对他现在的总好感度是 ${分}，不用像对主人那样亲近。）\n` + 文案
    }

    // ⚠ 权限：只有主人能让它动电脑。外人说的话，这一轮所有工具都会被闸掉。
    const 键 = 会话键(目标)
    挂工具闸(agent, 键)
    if (配置.外人只能聊天 && !主人说的) {
      外人轮.set(键, Date.now())
    } else {
      外人轮.delete(键)
    }

    try {
      当前说话人.set(键, { id: 谁id, name: 谁 || 聊天, 点我: !(来 && 来.atMe === false) })
      await agent.followup({
        id: 新id(),
        role: 'user',
        content: [{ type: 'text', text: 文案 }],
        source: { kind: 'wechat', fromMaster: 主人说的, sender: 谁id || 谁 || '' },
      })
      唤醒时间.set(会话键(目标), Date.now())
      轮来源.set(会话键(目标), 'wechat')
      回给表.set(会话键(目标), 聊天)
      try {
        if (ctx.sessions && typeof ctx.sessions.flush === 'function') await ctx.sessions.flush(agent.session)
      } catch (error) {}
      记账(`收到微信（${聊天}）→ 转进 DSH：「${掐短(文案, 50)}」`)
      return { ok: true, 说: '转进去了，它会照常开一轮' }
    } catch (error) {
      return { ok: false, 说: `followup 失败：${String((error && error.message) || error)}` }
    }
  }

  // ---------- 盯会话：把最后一句丢进队列 ----------

  function 收到事件(session, event) {
    const 配置 = 读配置()
    const 键 = 会话键(session && session.id)
    const 绑着 =
      (配置.绑定 || []).some((b) => 会话键(b.会话) === 键) ||
      (配置.默认会话 && 会话键(配置.默认会话) === 键)
    try {
      const 型 = event && event.type
      if (型 === 'turn/end' || 型 === 'assistant/message' || 型 === 'user/message') {
        记账(`[事件] ${型} 会话=${键 || '(空)'} 绑着=${绑着}`)
      }
    } catch (error) {}
    if (!绑着) return
    最后活动.set(键, Date.now())

    if (event && event.type === 'user/message') {
      const p = (event.data && (event.data.message || event.data)) || {}
      const src = p.source && p.source.kind ? String(p.source.kind) : ''
      if (src) 记账(`[来源] src=${src}`)
      // 主动开口的两种来路：插件自己的 speak-up（暂未用）、DSH 定时任务投递的 schedule。
      // schedule 轮的回复不能当「微信回话」发回原聊天，要走下面的「自己搭话」分支，发到主人私聊。
      if (src === 'speak-up' || src === 'schedule') 主动搭话轮.set(键, true)
      // 记住这一轮是谁开的头：只有微信开的头，回复才准发回微信。
      // 以前是「十分钟内有没有微信消息」，结果 Web 里聊天的回复被当成微信轮的回复发进群里。
      // 注意：runtime context 这类注入消息不带 source，不能拿它把来源冲掉，
      // 否则微信轮会被误判成「不是微信叫的」，回复就发不回去。
      // 2026-10-29：Web 和微信共用一个会话，后到的 web 来源会把 wechat 盖掉，
      // 于是微信里问的话回复发不回去。改成 wechat 优先：已经有 wechat 就不许被覆盖。
      // 2026-10-31：wechat 优先会「粘住」整轮不清 —— Web 里的回复被当成微信轮发进群里。
      // 改回「一轮由它的第一条用户消息定来源」：微信消息 → 微信轮，Web 输入 → Web 轮。
      if (!轮来源.has(键)) 轮来源.set(键, src)
      return
    }

    if (event && event.type === 'assistant/message') {
      const d = event.data || {}
      const 现 = 当前说话人.get(键)
      // 记到这一轮说话的人头上（没有就是本地直接聊的）
      try {
        if (d.usage) 记用量(现 ? 现.id : '', 现 ? 现.name : '本地（DSH 里直接聊）', d.usage)
      } catch (error) {}
      if (d.interrupted) {
        上一轮们.set(键, 空轮())
        轮来源.delete(键)
        return
      }
      const 摘 = 摘markdown图(提取正文(d.message) || '')
      let 文 = 洗掉代码和标签(摘.文本)
      const 图 = 提取图(d.message).concat(摘.图)
      // 它可以选择不回：正文里带了 [不回] 就整条丢掉。
      // 以前只认「整条正好等于 [不回]」，模型多写一个字就会把整段思考发到群里（2026-10-31 出过）。
      // 2026-11-14 又补一层：老逻辑要求「没点名」才丢，被点名的群里它写 [不回]，
      // 这三个字就直接发到群里了（22:45 那次）。现在看见标记先处理：
      // 整条只有标记 → 不发；标记夹在别的话里 → 没点名的群里整条丢，点名的摘掉标记再发。
      if (/\[不回\]/.test(文)) {
        const 摘掉标记 = 文.replace(/\[不回\]/g, '').trim()
        if (!摘掉标记 || (现 && 现.点我 === false)) {
          上一轮们.set(键, 空轮())
          return
        }
        文 = 摘掉标记
      }
      // 一轮里可能有好几条 assistant 消息（比如分两段说的两句话）：要拼起来，
      // 不能后一条盖掉前一条 —— 以前就是这么丢的，电脑上两句话、微信只剩一句。
      if (文 || 图.length) {
        const 旧 = 上一轮们.get(键)
        const 同轮 = 旧 && 旧.轮号 === d.turn
        const 拼文 = 同轮 && 旧.文本 ? (旧.文本 + '\n' + 文).trim() : 文
        const 拼图 = 同轮 ? [...(旧.图 || []), ...图] : 图
        上一轮们.set(键, { 轮号: d.turn, 文本: 拼文, 图: 拼图, 来源: 轮来源.get(键) || '' })
      }
      return
    }

    if (event && event.type === 'turn/end') {
      const d = event.data || {}
      const 完成 = !!(d.reason && d.reason.kind === 'completed')
      const 上一轮 = 上一轮们.get(键) || 空轮()
      const 有东西 = Boolean(上一轮.文本) || (上一轮.图 && 上一轮.图.length > 0)
      const 轮标 = 键 + ':' + String(d.turn)
      // 连着说话时，第二句常常被并进同一轮：assistant 记的轮号会和 turn/end 差一拍，
      // 以前是严格相等，差一拍就不发 —— 表现就是「第二句回复卡住没出去」。
      if (有东西 && 完成 && 上一轮.轮号 !== d.turn) {
        记账(`轮号对不上（assistant=${上一轮.轮号} / turn/end=${d.turn}），照发`)
      }
      if (完成 && 有东西 && (上一轮.轮号 === d.turn || 上一轮.轮号 <= d.turn) && !已发轮.has(轮标)) {
        已发轮.add(轮标)
        const 醒 = 唤醒时间.get(键) || 0
        // 只认「这一轮头一条用户消息来自微信」的轮；Web/CLI 自己聊的不许发出去。
        // 兜底：老逻辑的十分钟窗口只在完全没有来源信息时才用（兼容旧事件）。
        const 是微信叫的 = 上一轮.来源
          ? 上一轮.来源 === 'wechat'
          : 醒 > 0 && Date.now() - 醒 < 唤醒窗口
        唤醒时间.delete(键)
        轮来源.delete(键)
        if (是微信叫的) {
          if (!配置.总开关) {
            记账('这一轮是微信叫的，但总开关关着：不发')
          } else if (配置.试运行) {
            记账(`（试运行）该发到微信：「${掐短(上一轮.文本 || '（只有图）', 50)}」`)
          } else {
            const 回给 = 回给表.get(键) || ((配置.绑定 || [])[0] && (配置.绑定 || [])[0].聊天) || ''
            // 没有回给记录就别硬塞进队列：桥接那头只会回一句「没指定发给谁」，话就白丢了
            // （21:51 那次就是插件刚重载、回给表空着，一句「这句听懂了」发了个空 chat 出去）。
            if (!回给) {
              记账(`这一轮是微信叫的，但不知道回给谁（没有回给记录）：先不发`)
            } else {
            压队列({
              id: 新id(),
              text: 上一轮.文本 || '',
              images: 上一轮.图 || [],
              chat: 回给,
              at: Date.now(),
            })
            记账(
              `排队发到微信（${回给}）：「${掐短(上一轮.文本 || '（只有图）', 50)}」`
                + (上一轮.图 && 上一轮.图.length ? ` +${上一轮.图.length} 张图` : ''),
            )
            }
          }
        } else if (主动搭话轮.get(键)) {
          主动搭话轮.delete(键)
          // 自己起的头：话是说给主人听的，走他的私聊（不是最近那个群）
          // 发到 wxid 最稳：名字在库里必须能解析成 username 才发得出去，
          // 「主人」这种昵称在库里解析不出来（对方存的是备注），所以 wxid 优先，
          // 只有主人名单空着才退回显示名。
          const 主人聊天 = String((配置.主人 || [])[0] || '').trim() || String(配置.主人显示名 || '').trim()
          if (!配置.总开关) {
            记账('这一轮是自己搭话，但总开关关着：不发')
          } else if (!String(上一轮.文本 || '').trim() || /\[不回\]/.test(String(上一轮.文本 || ''))) {
            // 主动开口允许它「这次不想说话」：说了 [不回] 就整条丢掉，别让标记漏到微信里
            记账('自己搭话：这轮不想说，跳过')
          } else if (配置.试运行) {
            记账(`（试运行）自己搭话该发微信：「${掐短(上一轮.文本 || '（只有图）', 50)}」`)
          } else if (!桥进程) {
            记账('自己搭话：微信桥没在跑，不发')
          } else if (!主人聊天) {
            记账('自己搭话：设置里没填主人的微信名，不知道发给谁')
          } else {
            压队列({ id: 新id(), text: 上一轮.文本 || '', images: [], chat: 主人聊天, at: Date.now() })
            记账(`自己搭话 → 发到微信（${主人聊天}）：「${掐短(上一轮.文本 || '（只有图）', 50)}」`)
          }
        }
      }
      上一轮们.set(键, 空轮())
      当前说话人.delete(键)
      外人轮.delete(键)      // 这一轮完了，工具闸抬起来
      if (已发轮.size > 300) {
        const 全 = [...已发轮]
        for (const x of 全.slice(0, 全.length - 300)) 已发轮.delete(x)
      }
    }
  }

  // ---------- Python 那半 ----------

  const 进程日志 = []
  const 记进 = (t) => {
    for (const 行 of String(t).split(/\r?\n/)) {
      if (!行.trim()) continue
      进程日志.push(`[${时间戳()}] ${行}`)
    }
    环形(进程日志, 进程日志上限)
  }

  let 选定python = ''
  const 桥python = () => 选定python || 读配置().解释器 || ''

  /**
   * 挑一个真能跑的 Python 来起桥接：先看设置里填的，再扫本机装过的。
   * 不限制版本，只认「能 import wechatauto 和 numpy」—— 环境由使用者自己装。
   */
  const 挑python = async () => {
    const 配置 = 读配置()
    const 候选 = [配置.解释器, ...扫Python(), 'py', 'python3', 'python'].filter(Boolean)
    for (const c of 候选) {
      try {
        if (await 能跑(c, 'import wechatauto, numpy; print("OK")')) {
          return { py: c, 说: '' }
        }
      } catch (error) {}
    }
    return {
      py: '',
      说: '没找到装好 wechatauto 和 numpy 的 Python —— 先自己把这两个包装好，或去设置里填「解释器路径」',
    }
  }

  let 桥进程 = null
  let 桥进程启动于 = 0
  let 启动中 = false

  /** 让一个 python 跑一行代码，看它行不行（能 import 且打印 OK）。 */
  const 能跑 = (py, 代码) =>
    new Promise((resolve) => {
      execFile(py, ['-c', 代码], { timeout: 25000, windowsHide: true }, (err, stdout) => {
        resolve(!err && String(stdout).includes('OK'))
      })
    })

  // 扫一遍本机装过的 Python，按版本从高到低排。
  // 不写死版本号：3.16、3.17 出来后不用再来改这里。
  const 扫Python = () => {
    const 找到 = []
    const 扫一个 = (底, 图案) => {
      let 目录们 = []
      try { 目录们 = readdirSync(底) } catch { return }
      for (const 名 of 目录们) {
        const m = 名.match(图案)
        if (!m) continue
        const 路 = join(底, 名, 'python.exe')
        if (!existsSync(路)) continue
        找到.push({ 分: Number(m[1]) * 100 + Number(m[2]), 路 })
      }
    }
    const 本用户 = process.env.LOCALAPPDATA || ''
    // 官方安装器的两种落点：pythoncore-3.15-64 和 Programs\Python\Python315
    扫一个(join(本用户, 'Python'), /^pythoncore-(\d+)\.(\d+)-\d+$/)
    扫一个(join(本用户, 'Programs', 'Python'), /^Python(\d)(\d+)$/)
    扫一个(join(process.env.ProgramFiles || 'C:\\Program Files', 'Python'), /^Python(\d)(\d+)$/)
    return 找到.sort((a, b) => b.分 - a.分).map((x) => x.路)
  }

  const 启动桥接 = async () => {
    if (桥进程) return { ok: false, 说: '桥接已经在跑了' }
    if (启动中) return { ok: false, 说: '桥接正在启动，请稍等' }
    启动中 = true
    try {
      const 挑 = await 挑python()
      const py = 挑.py
      选定python = py
      if (挑.说) 记账('桥接 Python：' + 挑.说)
      if (!py) {
        return {
          ok: false,
          说: '还没有能跑的 Python 环境 —— 先自己装好 Python 和 wechatauto、numpy；或者去设置里填「解释器路径」',
        }
      }
      if (!existsSync(桥脚本)) return { ok: false, 说: `找不到桥接脚本：${桥脚本}` }
      let origin = ''
      try {
        origin = String(ctx.webServer.getOrigin ? ctx.webServer.getOrigin() : '').replace(/\/$/, '')
      } catch (error) {}
      if (!origin) return { ok: false, 说: '拿不到 DSH 自己的地址（webServer.getOrigin 失败）' }

      const 配置 = 读配置()
      const 参数 = [桥脚本, '--dsh', origin]
      if (配置.令牌) 参数.push('--token', 配置.令牌)

      // 嵌入式 Python 在 C 盘 TEMP 上可能被拦，给它一个自家目录当临时目录
      const 临时目录 = join(目录, 'wx-tmp')
      try {
        mkdirSync(临时目录, { recursive: true })
      } catch (error) {}

      const 本进程 = spawn(py, 参数, {
        windowsHide: true,
        env: {
          ...process.env,
          PYTHONIOENCODING: 'utf-8',
          HTTP_PROXY: '',
          HTTPS_PROXY: '',
          ALL_PROXY: '',
          TEMP: 临时目录,
          TMP: 临时目录,
        },
      })
      桥进程 = 本进程
      桥进程启动于 = Date.now()
      本进程.stdout.on('data', (d) => 记进(String(d).trim()))
      本进程.stderr.on('data', (d) => 记进(String(d).trim()))
      本进程.on('exit', (code) => {
        记进(`[进程退出] code=${code}`)
        记账(`桥接进程退出了（code=${code}）`)
        // 只有退出的仍是当前这个进程才清掉引用：否则 stop→start 后旧进程退出会把新进程弄丢
        if (桥进程 === 本进程) 桥进程 = null
      })
      本进程.on('error', (e) => {
        记进(`[进程出错] ${String((e && e.message) || e)}`)
        if (桥进程 === 本进程) 桥进程 = null
      })
      记账(`桥接已启动 → ${origin}`)
      return { ok: true, 说: '启动了，看下面日志' }
    } finally {
      启动中 = false
    }
  }

  const 停止桥接 = () => {
    if (!桥进程) return { ok: false, 说: '桥接没在跑' }
    try {
      桥进程.kill()
    } catch (error) {}
    桥进程 = null
    记账('桥接已停止')
    return { ok: true, 说: '停了' }
  }

  const 桥状态 = () => ({
    运行中: Boolean(桥进程),
    运行秒: 桥进程 ? Math.round((Date.now() - 桥进程启动于) / 1000) : 0,
    进程日志: 进程日志.slice(-80),
    桥脚本,
    python: 选定python || 桥python(),
  })

  // ---------- HTTP ----------

  const 回 = (res, o) => {
    try {
      res.setHeader?.('content-type', 'application/json; charset=utf-8')
      res.end?.(JSON.stringify(o))
    } catch (error) {}
  }

  const 读体 = async (req) => {
    let body = ''
    try {
      for await (const chunk of req) body += chunk
    } catch (error) {}
    try {
      return JSON.parse(body || '{}')
    } catch (error) {
      return {}
    }
  }

  const 查令牌 = (req, 体) => {
    const 配置 = 读配置()
    if (!配置.令牌) return true
    const 头 = String((req.headers && (req.headers['x-bridge-token'] || req.headers.authorization)) || '')
    const 带的 = 头.replace(/^Bearer\s+/i, '') || String((体 && 体.token) || '')
    return 带的 === 配置.令牌
  }

  const 路线 = (路径, handler) =>
    ctx.effect(() =>
      ctx.webServer.register({
        kind: 'exact',
        path: 路径,
        handler: async (req, res) => {
          try {
            await handler(req, res)
          } catch (error) {
            回(res, { ok: false, 说: String((error && error.message) || error) })
          }
        },
      }),
    )

  const 概况 = async () => {
    const 配置 = 读配置()
    const 清单 = 会话清单()
    const 标题表 = new Map(清单.map((x) => [会话键(x.id), x.标题 || ('…' + String(x.id).slice(-8))]))
    let origin = ''
    try {
      origin = String(ctx.webServer.getOrigin ? ctx.webServer.getOrigin() : '')
    } catch (error) {}
    return {
      ok: true,
      版本: 插件版本,
      仓库: 仓库地址,
      配置: {
        ...配置,
        令牌: 配置.令牌 ? '（已设）' : '',
        视觉密钥: 配置.视觉密钥 ? '（已设）' : '',
        视觉备用密钥: 配置.视觉备用密钥 ? '（已设）' : '',
      },
      配置文件,
      DSH地址: origin,
      活着的会话: 清单,
      绑定情况: (配置.绑定 || []).map((b) => ({
        ...b,
        会话标题: 标题表.get(会话键(b.会话)) || '',
        活着: 清单.some((x) => 会话键(x.id) === 会话键(b.会话)),
      })),
      最近聊天: 最近聊天列(),
      待发队列: outbox.length,
      桥接: 桥状态(),
      掉线: 掉线, // 桥接最近一次汇报（微信退了要人登录）；null = 没有
      用量: 读用量(),
      日志,
    }
  }

  // —— 桥接用 ——
  路线(`${路由前缀}/receive`, async (req, res) => {
    const 来 = await 读体(req)
    if (!查令牌(req, 来)) return 回(res, { ok: false, 说: '令牌不对' })
    return 回(res, await 转入消息(来))
  })

  路线(`${路由前缀}/outbox`, async (req, res) => {
    if (!查令牌(req, null)) return 回(res, { ok: false, 说: '令牌不对' })
    const 给 = outbox.splice(0, outbox.length)
    return 回(res, { ok: true, 条数: 给.length, 消息: 给 })
  })

  路线(`${路由前缀}/outbox/ack`, async (req, res) => {
    const 来 = await 读体(req)
    const 成 = 来 && 来.ok !== false
    // 真发成功一条，就说明微信活得好好的 —— 把之前那条「掉线」清掉。
    // 不清的后果：一次误报（窗口最小化被当成不在）之后，状态栏会一直挂着「微信掉线」，要重启 DSH 才消失。
    if (成) 掉线 = null
    记账(成 ? `微信发送成功（${来.chat || '?'}）` : `微信发送失败（${来.chat || '?'}）：${掐短(来.说 || '原因不明', 80)}`)
    return 回(res, { ok: true })
  })

  // —— 设置页用 ——
  路线(`${路由前缀}/state`, async (req, res) => {
    await 读体(req)
    return 回(res, await 概况())
  })

  路线(`${路由前缀}/save`, async (req, res) => {
    const 来 = await 读体(req)
    const 旧 = 读配置()
    const 来配 = 来 && typeof 来 === 'object' ? { ...来 } : {}
    // 页面拿到的密钥是「（已设）」：没改就保留旧值，别把真钥匙覆盖成三个字
    for (const k of ['令牌', '视觉密钥', '视觉备用密钥']) {
      if (来配[k] === '（已设）') 来配[k] = 旧[k]
    }
    const 新 = 归一化({ ...旧, ...来配 })
    if (!写配置(新)) return 回(res, { ok: false, 说: `写不进配置文件：${配置文件}` })
    // 少写半边（只填了聊天名没选会话，或者反过来）的行会被丢掉 —— 别让它在页面上无声消失。
    const 来绑 = Array.isArray(来配.绑定) ? 来配.绑定 : []
    const 丢行 = 来绑
      .map((x, i) => ({ i: i + 1, 聊天: String((x && x.聊天) || '').trim(), 会话: String((x && x.会话) || '').trim() }))
      .filter((x) => !(x.聊天 && x.会话))
    const 尾 = 丢行.length
      ? `；第 ${丢行.map((x) => x.i).join('、')} 行没填全（聊天名 / 会话），没收`
      : ''
    记账(`设置已保存：绑定 ${新.绑定.length} 条｜${新.总开关 ? (新.试运行 ? '试运行' : '真发') : '总开关关着'}${尾}`)
    return 回(res, {
      ...(await 概况()),
      丢掉绑定: 丢行,
      说: `保存好了（绑定 ${新.绑定.length} 条、不回 ${新.屏蔽.length} 个）${尾}`,
    })
  })

  路线(`${路由前缀}/test-send`, async (req, res) => {
    const 来 = await 读体(req)
    const 配置 = 读配置()
    const 聊天 = String(来.聊天 || ((配置.绑定 || [])[0] && (配置.绑定 || [])[0].聊天) || '')
    if (!聊天) return 回(res, { ok: false, 说: '还没绑定任何聊天 —— 先加一条绑定' })
    const 文字 = '（这是一条测试消息：DSH → 微信通了）'
    压队列({ id: 新id(), text: 文字, chat: 聊天, at: Date.now() })
    记账(`手动测试：已排队发给「${聊天}」`)
    return 回(res, { ok: true, 说: `排好了，桥接两秒内会发到「${聊天}」` })
  })

  路线(`${路由前缀}/send`, async (req, res) => {
    const 来 = await 读体(req)
    const 聊天 = String((来 && 来.聊天) || '').trim()
    const 文字 = String((来 && 来.文本) || '').trim()
    if (!聊天) return 回(res, { ok: false, 说: '缺 聊天' })
    const 图 = Array.isArray(来 && 来.图片) ? 来.图片.map(String).filter(Boolean) : []
    if (!文字 && !图.length) return 回(res, { ok: false, 说: '缺 文本' })
    压队列({ id: 新id(), text: 文字, images: 图, chat: 聊天, at: Date.now() })
    记账(`手动发微信（${聊天}）：${掐短(文字, 40)}`)
    return 回(res, { ok: true, 说: `排好了，桥接两秒内发到「${聊天}」` })
  })

  路线(`${路由前缀}/proactive-now`, async (req, res) => {
    // 自检用：不管闲了多久、也不管间隔，现在就让「主动对话」开一轮
    return 回(res, await 主动开口一次(true))
  })

  路线(`${路由前缀}/notify`, async (req, res) => {
    // 桥接有事要告诉主人时走这儿（目前只有「微信掉线，等我扫码」）。
    // 只写日志、只存一条状态，**不**去开一轮会话 —— 微信都断了，让她开口也是白发。
    const 来 = await 读体(req)
    // 桥接那边发的是英文键 text（跟 /receive 一路），手工测用中文「文本」也行
    const 话 = String((来 && (来.文本 || 来.text)) || '').trim()
    if (!话) return 回(res, { ok: false, 说: '缺 文本' })
    掉线 = { 文本: 话, 时间: Date.now() }
    记账(`桥接汇报（不自动处理）：${掐短(话, 60)}`)
    return 回(res, { ok: true })
  })

  路线(`${路由前缀}/test-inbound`, async (req, res) => {
    const 来 = await 读体(req)
    return 回(
      res,
      await 转入消息({
        text: String(来.text || '（测试）DSH 这边收到微信消息了吗'),
        chat: String(来.聊天 || '测试'),
        chatType: 'private',
        sender: '测试',
        图片: Array.isArray(来.图片) ? 来.图片 : [],
        图片类型: String(来.图片类型 || '图片'),
      }),
    )
  })

  路线(`${路由前缀}/vision-test`, async (req, res) => {
    const 来 = await 读体(req)
    const 图 = (Array.isArray(来 && 来.图片) ? 来.图片 : [来 && 来.图片])
      .map((x) => String(x == null ? '' : x).trim())
      .filter(Boolean)
    if (!图.length) return 回(res, { ok: false, 说: '缺 图片（本地路径）' })
    try {
      const 描述 = await 描述图片们(图, String((来 && 来.类型) || '图片'), 读配置(), 记账)
      return 回(res, { ok: !!描述, 描述, 说: 描述 ? '看完了' : '没看清（检查视觉密钥 / 图片格式）' })
    } catch (error) {
      return 回(res, { ok: false, 说: String((error && error.message) || error) })
    }
  })

  // —— 版本检查（「关于」页用）——

  // 仓库地址从 package.json 的 repository 读：换仓库名只改那一处，别在代码里再写死一份
  const 仓库地址 = (() => {
    try {
      const 原文 = String(readFileSync(join(插件根, 'package.json'), 'utf8')).replace(/^\uFEFF/, '')
      const 仓 = JSON.parse(原文).repository
      const 址 = String((仓 && 仓.url) || (typeof 仓 === 'string' ? 仓 : '')).replace(/^git\+/, '').replace(/\.git$/, '')
      return 址 || 'https://github.com/izhtes/dsh-wechat-link'
    } catch (error) {
      return 'https://github.com/izhtes/dsh-wechat-link'
    }
  })()
  const 远端清单 = (() => {
    const m = /^https:\/\/github\.com\/([^/]+)\/([^/]+)$/.exec(仓库地址)
    return m ? `https://raw.githubusercontent.com/${m[1]}/${m[2]}/main/package.json` : ''
  })()

  const 比版本 = (a, b) => {
    const 拆 = (v) => String(v || '').replace(/^v/i, '').split('.').map((x) => parseInt(x, 10) || 0)
    const x = 拆(a)
    const y = 拆(b)
    for (let i = 0; i < Math.max(x.length, y.length); i++) {
      const d = (x[i] || 0) - (y[i] || 0)
      if (d) return d > 0 ? 1 : -1
    }
    return 0
  }

  路线(`${路由前缀}/check-update`, async (req, res) => {
    await 读体(req)
    const 当前 = 插件版本
    if (!远端清单) return 回(res, { ok: false, 当前, 说: 'package.json 里没写 repository，检查更新用不了' })
    try {
      const r = await fetch(远端清单, {
        headers: { 'user-agent': 'dsh-wechat-link' },
        signal: AbortSignal.timeout(8000),
      })
      if (!r.ok) return 回(res, { ok: false, 当前, 说: '拉不到 GitHub 上的版本文件（HTTP ' + r.status + '）' })
      const d = await r.json()
      const 最新 = String((d && d.version) || '')
      if (!最新) return 回(res, { ok: false, 当前, 说: '远端文件里没有版本号' })
      return 回(res, { ok: true, 当前, 最新, 有更新: 比版本(最新, 当前) > 0, 仓库: 仓库地址 })
    } catch (error) {
      return 回(res, { ok: false, 当前, 说: '连不上 GitHub：' + ((error && error.message) || '超时') })
    }
  })

  // —— 依赖 / 进程 ——
  路线(`${路由前缀}/bridge/status`, async (req, res) => {
    await 读体(req)
    return 回(res, { ok: true, ...桥状态() })
  })

  路线(`${路由前缀}/bridge/start`, async (req, res) => {
    await 读体(req)
    return 回(res, await 启动桥接())
  })

  路线(`${路由前缀}/bridge/stop`, async (req, res) => {
    await 读体(req)
    return 回(res, 停止桥接())
  })

  // ---------- 工具：让它自己管「绑定」和「不回谁」 ----------
  //
  // 设置页是给人点的；这两条是给 AI 伙伴用的 —— 主人在聊天里说一句
  // 「把三班那群绑到工作那间」「以后别理老李」，它自己就把配置改了。

  const 存一版 = (补) => {
    const 旧 = 读配置()
    const 新 = 归一化({ ...旧, ...补 })
    return 写配置(新) ? 新 : null
  }

  const 猜会话 = (给) => {
    const 想 = String(给 == null ? '' : 给).trim()
    if (!想) return ''
    const 清单 = 会话清单()
    const 键 = 会话键(想)
    const 正 = 清单.find((x) => String(x.id) === 想 || 会话键(x.id) === 键)
    if (正) return String(正.id)
    const 像 = 清单.filter((x) => x.标题 && String(x.标题).includes(想))
    return 像.length === 1 ? String(像[0].id) : ''
  }

  const 会话标题Of = (id) => {
    const 键 = 会话键(id)
    const 中 = 会话清单().find((x) => 会话键(x.id) === 键)
    return (中 && 中.标题) || '…' + 键.slice(-8)
  }

  const 挂工具 = () => {
    const 参数表 = (o) => ({ type: 'object', properties: o, additionalProperties: false })
    const 输出 = {
      schema: {
        type: 'object',
        properties: { ok: { type: 'boolean' }, 说: { type: 'string' } },
        required: ['ok', '说'],
      },
      render: (_参数, 值) => [{ type: 'text', text: String((值 && 值.说) || '') }],
    }

    try {
      ctx.tools.register({
        name: 'wechat_block',
        description:
          '微信里的「不回谁」：把某个聊天 / 某人加进或移出屏蔽名单（这些人、这群的消息不进对话，也不写群日记）。'
          + '改完桥接最多 30 秒就认。不给 action 就是 add；想看看现在都不回谁，用 list。',
        parameters: 参数表({
          who: { type: 'string', description: '微信里显示的名字（好友昵称 / 群名），也认 wxid' },
          action: { type: 'string', enum: ['add', 'remove', 'list'], description: '默认 add' },
        }),
        output: 输出,
        execute: async (参数) => {
          const 动 = String((参数 && 参数.action) || 'add').trim().toLowerCase()
          const 谁 = String((参数 && 参数.who) || '').trim()
          const 名单 = [...(读配置().屏蔽 || [])]
          if (动 === 'list' || !谁) {
            return {
              ok: true,
              说: 名单.length ? `现在不回这 ${名单.length} 个：${名单.join('、')}` : '屏蔽名单是空的 —— 谁的都回。',
            }
          }
          if (动 === 'remove' || 动 === 'del' || 动 === 'delete') {
            const 剩 = 名单.filter((x) => x !== 谁)
            if (剩.length === 名单.length) return { ok: false, 说: `名单里没有「${谁}」` }
            const 新 = 存一版({ 屏蔽: 剩 })
            if (!新) return { ok: false, 说: `配置写不进去：${配置文件}` }
            记账(`不回谁 -1：「${谁}」（剩 ${新.屏蔽.length} 个）`)
            return { ok: true, 说: `把「${谁}」放回来了，以后他/它说话照常进对话。现在不回 ${新.屏蔽.length} 个。` }
          }
          if (名单.includes(谁)) return { ok: true, 说: `「${谁}」本来就不回（名单共 ${名单.length} 个）` }
          const 新 = 存一版({ 屏蔽: [...名单, 谁] })
          if (!新) return { ok: false, 说: `配置写不进去：${配置文件}` }
          记账(`不回谁 +1：「${谁}」（共 ${新.屏蔽.length} 个）`)
          return { ok: true, 说: `以后「${谁}」发消息不进对话了，也不写日记。现在不回 ${新.屏蔽.length} 个：${新.屏蔽.join('、')}` }
        },
      })
      记账('工具 wechat_block 挂上了')
    } catch (error) {
      记账('工具 wechat_block 挂不上：' + String((error && error.message) || error))
    }

    try {
      ctx.tools.register({
        name: 'wechat_bind',
        description:
          '微信「单独绑定」：把某个微信聊天（好友 / 群）绑到某间 DSH 会话，它来的话就进那间；没绑的走「默认会话」。'
          + 'action=list 看现在绑了哪些、最近谁来找过；session 可以填会话 id 或标题里的几个字，不填 = 默认会话。',
        parameters: 参数表({
          chat: { type: 'string', description: '微信里显示的名字（好友昵称 / 群名），也认 wxid' },
          session: { type: 'string', description: '会话 id 或标题片段；不填 = 默认会话' },
          action: { type: 'string', enum: ['add', 'remove', 'list'], description: '默认 add' },
        }),
        output: 输出,
        execute: async (参数) => {
          const 动 = String((参数 && 参数.action) || 'add').trim().toLowerCase()
          const 聊天 = String((参数 && 参数.chat) || '').trim()
          const 配置 = 读配置()
          if (动 === 'list' || (!聊天 && 动 !== 'remove')) {
            const 绑定表 = 配置.绑定 || []
            const 绑 = 绑定表.length
              ? 绑定表.map((b) => `「${b.聊天}」→ ${会话标题Of(b.会话)}`).join('\n')
              : '（还没单独绑定，全走默认会话）'
            const 最近 = 最近聊天列()
              .slice(0, 12)
              .map((x) => x.名)
              .join('、')
            return {
              ok: true,
              说:
                `${绑}\n默认会话：${配置.默认会话 ? 会话标题Of(配置.默认会话) : '（没设）'}`
                + `${最近 ? `\n最近来找过的：${最近}` : ''}`,
            }
          }
          if (动 === 'remove' || 动 === 'del' || 动 === 'delete') {
            const 旧表 = 配置.绑定 || []
            const 剩 = 旧表.filter((b) => b.聊天 !== 聊天)
            if (剩.length === 旧表.length) return { ok: false, 说: `没绑过「${聊天}」` }
            const 新 = 存一版({ 绑定: 剩 })
            if (!新) return { ok: false, 说: `配置写不进去：${配置文件}` }
            记账(`解绑：「${聊天}」（剩 ${新.绑定.length} 条）`)
            return { ok: true, 说: `「${聊天}」解绑了，以后走默认会话。还剩 ${新.绑定.length} 条绑定。` }
          }
          const 想 = String((参数 && 参数.session) || '').trim()
          const 会话 = 想 ? 猜会话(想) : 配置.默认会话
          if (想 && !会话) {
            const 有名 = 会话清单().map((x) => x.标题 || x.id).join('、')
            return { ok: false, 说: `找不到「${想}」这间会话。现在有的是：${有名 || '（一间都没有）'}` }
          }
          if (!会话) return { ok: false, 说: '还没有会话能绑：先在 DSH 里开一间，或者去设置里选「默认会话」' }
          const 新表 = (配置.绑定 || []).filter((b) => b.聊天 !== 聊天).concat([{ 聊天, 会话 }])
          const 新 = 存一版({ 绑定: 新表 })
          if (!新) return { ok: false, 说: `配置写不进去：${配置文件}` }
          记账(`绑定：「${聊天}」→ ${会话标题Of(会话)}（共 ${新.绑定.length} 条）`)
          return {
            ok: true,
            说: `绑好了：「${聊天}」以后进「${会话标题Of(会话)}」。现在一共 ${新.绑定.length} 条绑定。`,
          }
        },
      })
      记账('工具 wechat_bind 挂上了')
    } catch (error) {
      记账('工具 wechat_bind 挂不上：' + String((error && error.message) || error))
    }

    try {
      ctx.tools.register({
        name: 'wechat_send',
        description:
          '主动往微信里的某个聊天发一句话（好友 / 群，填微信里显示的名字，也认 wxid / xxx@chatroom）。'
          + '平时回复会自动回到「刚才说话的人」；这个工具是给「人不在这个聊天里，也要往某个群丢一句」用的。'
          + '排进桥接队列，两秒内送达；长文由桥接拆成几条短消息。',
        parameters: 参数表({
          聊天: { type: 'string', description: '微信里显示的名字（好友昵称 / 群名），也认 wxid / xxx@chatroom' },
          文本: { type: 'string', description: '要说的话（纯文本）' },
        }),
        output: 输出,
        execute: async (参数) => {
          const 聊天 = String((参数 && 参数.聊天) || '').trim()
          const 文本 = String((参数 && 参数.文本) || '').trim()
          if (!聊天) return { ok: false, 说: '缺 聊天（发到哪个聊天）' }
          if (!文本) return { ok: false, 说: '缺 文本（要说什么）' }
          if (!读配置().总开关) return { ok: false, 说: '总开关关着 —— 先去设置里打开' }
          压队列({ id: 新id(), text: 文本, images: [], chat: 聊天, at: Date.now() })
          记账(`主动发微信（${聊天}）：${掐短(文本, 40)}`)
          return { ok: true, 说: `排好了，桥接两秒内发到「${聊天}」` }
        },
      })
      记账('工具 wechat_send 挂上了')
    } catch (error) {
      记账('工具 wechat_send 挂不上：' + String((error && error.message) || error))
    }
  }

  挂工具()

  // ---------- 挂监听 ----------

  ctx.effect(() => {
    let 摘
    try {
      摘 = ctx.on('session/event', (session, event) => {
        try {
          收到事件(session, event)
        } catch (error) {
          记账(`盯会话出错：${String((error && error.message) || error)}`)
        }
      })
      记账('session/event 订阅成功')
    } catch (error) {
      记账('session/event 订阅失败：' + String((error && error.message) || error))
      return
    }
    return () => {
      try {
        if (typeof 摘 === 'function') 摘()
      } catch (error) {}
    }
  }, 'wechat-bridge: 盯伙伴说话')
  记账('已挂 session/event 监听')

  // ---------- 主动对话（没人找它，它也会自己开口）----------

  /** 上一次主动开口的时间：重载后从当下算起，别一启动就冲到微信里说话 */
  let 上次主动 = Date.now()

  async function 主动开口一次(强制 = false) {
    const 配置 = 读配置()
    if (!配置.主动对话 || !配置.总开关) return { ok: false, 说: '主动对话没开 / 总开关关着' }
    const 起 = Number(配置.主动时段起)
    const 止 = Number(配置.主动时段止)
    const h = new Date().getHours()
    const 在时段 = 起 === 止 ? true : 起 < 止 ? h >= 起 && h < 止 : h >= 起 || h < 止
    if (!在时段 && !强制) return { ok: false, 说: `现在 ${h} 点，不在主动时段里` }
    const 隔 = Math.max(5, Number(配置.主动间隔分钟) || 90) * 60 * 1000
    const 现在 = Date.now()
    if (!强制 && 现在 - 上次主动 < 隔) return { ok: false, 说: '距上次主动还不够一个间隔' }
    const 目标 = String(配置.默认会话 || (配置.绑定[0] && 配置.绑定[0].会话) || '').trim()
    if (!目标) return { ok: false, 说: '没有默认会话' }
    const 键 = 会话键(目标)
    // 刚聊过就别打扰：这间会话安静满一个间隔才算「闲下来」
    if (!强制 && 现在 - (最后活动.get(键) || 0) < 隔) return { ok: false, 说: '这间会话还热着，等它安静下来' }
    // 正在说话就别插嘴
    if (!强制 && 轮来源.has(键)) return { ok: false, 说: '这间会话正在说话' }
    let agent = 找agent(目标)
    if (!agent) {
      try {
        await ctx.agents.resume({ resumeSessionId: 目标 })
        agent = 找agent(目标)
      } catch (error) {}
    }
    if (!agent) {
      记账(`主动对话：会话 ${目标} 没开着，这一轮跳过`)
      return { ok: false, 说: `会话 ${目标} 没开着` }
    }
    const 提示 =
      String(配置.主动提示 || '').trim()
      || '（你自己待了一阵子，忽然想起主人了。想说话就说一句短的、像平时发微信那样；'
        + '不想说就只输出 [不回]，别解释，也别提「定时」「主动」这类词。）'
    上次主动 = 现在
    记账('主动对话：自己开一轮')
    try {
      await agent.followup({
        id: 新id(),
        role: 'user',
        content: [{ type: 'text', text: 提示 }],
        source: { kind: 'speak-up' },
      })
      // 不在这里写 轮来源 / 唤醒时间：等 user/message 事件把来源记成 speak-up 再走「自己搭话」分支。
      // 提前写反而会在「这一轮没起来」时留下脏标记，把后面的主动对话一直挡掉。
      return { ok: true, 说: '开了一轮，看宿主日志里「自己搭话」那一行' }
    } catch (error) {
      记账(`主动对话失败：${String((error && error.message) || error)}`)
      return { ok: false, 说: `followup 失败：${String((error && error.message) || error)}` }
    }
  }

  ctx.effect(() => {
    const 计时 = setInterval(() => {
      主动开口一次().catch(() => {})
    }, 60 * 1000)
    // 不让这个后台计时把宿主 / 自测脚本的进程拖住（有需要时它照样会走）
    计时.unref?.()
    return () => clearInterval(计时)
  }, 'wechat-bridge: 主动对话')

  ctx.effect(() => () => {
    try {
      if (桥进程) 桥进程.kill()
    } catch (error) {}
  }, 'wechat-bridge: 收尾')

  try {
    if (!existsSync(配置文件)) 写配置(读配置())
  } catch (error) {
    写配置(读配置())
  }

  const 配置 = 读配置()
  记账(
    `已装好 v${插件版本}｜总开关 ${配置.总开关 ? '开' : '关'}｜${配置.试运行 ? '试运行' : '真发'}｜`
      + `绑定 ${配置.绑定.length} 条｜不回 ${配置.屏蔽.length} 个`,
  )
  自检补丁()

  return { 记账, 概况, 转入消息, 收到事件, 启动桥接, 停止桥接, 桥状态, 日志 }}
