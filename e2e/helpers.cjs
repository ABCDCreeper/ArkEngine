/**
 * E2E 公共工具：浏览器解析、登录、接口调用与断言辅助。
 *
 * Playwright 的解析顺序：环境变量 PLAYWRIGHT_PATH → 本仓库 node_modules → 全局。
 * 这样本地（可能有独立的运行时目录）与 CI（用 npm 装的 playwright）都能跑。
 */
const assert = require('node:assert/strict')
const path = require('node:path')

function loadPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_PATH,
    'playwright',
    'playwright-core',
  ].filter(Boolean)
  for (const candidate of candidates) {
    try {
      return require(candidate)
    } catch {
      /* 继续尝试下一个 */
    }
  }
  throw new Error('未找到 playwright，请先安装：npm i -D playwright && npx playwright install chromium')
}

const { chromium } = loadPlaywright()

// 默认指向后端单进程模式（-Prod）的地址；也可用环境变量覆盖
const base = (process.env.INNOARK_URL || 'http://127.0.0.1:5000').replace(/\/$/, '')
const shotsDir = process.env.INNOARK_SHOTS || path.join(process.cwd(), '.runtime')

async function launch(options = {}) {
  const browser = await chromium.launch({
    headless: true,
    // CI 里用装好的 chromium；本地有 Chrome 时优先用 Chrome
    channel: process.env.PLAYWRIGHT_CHANNEL || (process.env.CI ? undefined : 'chrome'),
  })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    ...options,
  })
  return { browser, context }
}

/** 收集页面脚本错误与 4xx/5xx 响应，供断言"无错误"用 */
function watch(page) {
  const errors = []
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`))
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`console: ${msg.text()}`)
  })
  return errors
}

async function api(pathname, { method = 'GET', token, body } = {}) {
  const res = await fetch(base + pathname, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await res.text()
  return { status: res.status, body: text ? JSON.parse(text) : null }
}

async function login(username, password = '123456') {
  const res = await api('/api/sessions', { method: 'POST', body: { username, password } })
  assert.equal(res.status, 201, `登录失败 ${username}: ${JSON.stringify(res.body)}`)
  return res.body
}

/** 在页面里完成登录（走真实表单，顺带覆盖登录流程本身） */
async function signIn(page, username, password = '123456') {
  await page.goto(base + '/login')
  await page.evaluate(() => localStorage.clear())
  await page.goto(base + '/login')
  await page.locator('input').nth(0).fill(username)
  await page.locator('input').nth(1).fill(password)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL((url) => !url.pathname.includes('login'))
}

/** 用给定账号的 token 替换当前登录态（用于同页切换身份） */
async function switchUser(page, session) {
  await page.evaluate((s) => {
    localStorage.setItem('innoark_token', s.token)
    localStorage.setItem('innoark_user', JSON.stringify(s.user))
  }, session)
  await page.reload()
}

/** 走前端路由跳转；page.goto 会整页重载，销毁内存状态，与真实使用不符 */
async function pushRoute(page, route) {
  await page.evaluate((r) => {
    const router = document.querySelector('#app').__vue_app__.config.globalProperties.$router
    return router.push(r)
  }, route)
}

module.exports = { assert, base, shotsDir, launch, watch, api, login, signIn, switchUser, pushRoute }
