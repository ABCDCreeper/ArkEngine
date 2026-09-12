/**
 * 端到端主流程：学生 → 教师 → 管理角色，覆盖过程记录、评价、判分与页面健康。
 *
 * 运行前需先启动服务（后端单进程模式会一并提供前端页面）：
 *   ./start.ps1 -Prod -Demo   然后 node e2e/flows.cjs
 * 也可指定地址：INNOARK_URL=http://127.0.0.1:5173 node e2e/flows.cjs
 */
const { assert, base, launch, watch, api, login, signIn, switchUser } = require('./helpers.cjs')

const results = []
const pass = (label) => { results.push(label); console.log('  ✓ ' + label) }

;(async () => {
  const { browser, context } = await launch()
  const pageErrors = []
  let page = null
  try {
    const project = await api('/api/projects', {
      method: 'POST', token: (await login('student')).token,
      body: { topicId: 'topic1', name: `E2E-${Date.now()}` },
    })
    assert.equal(project.status, 201, JSON.stringify(project.body))
    const pid = project.body.id
    let student = await login('student')
    const teacher = await login('teacher')

    // ---------------------------------------------------------------- 学生：任务全流程
    page = await context.newPage()
    pageErrors.push(...watch(page))
    await signIn(page, 'student')

    const annotation = await api(`/api/projects/${pid}/annotations`, {
      method: 'POST', token: teacher.token, body: { content: 'E2E 批注：补充量化结论' },
    })
    assert.equal(annotation.status, 201, JSON.stringify(annotation.body))

    // 教师把批注转成任务（带验收标准）
    const derived = await api(`/api/annotations/${annotation.body.id}/tasks`, {
      method: 'POST', token: teacher.token,
      body: { title: 'E2E 派生任务', assigneeId: 'u1', criteria: ['有数据', '有结论'] },
    })
    assert.equal(derived.status, 201, JSON.stringify(derived.body))
    const taskId = derived.body.id
    // 先确认后端确实保存了验收标准，把"后端没存"和"前端没渲染"分开
    assert.ok(String(derived.body.criteria || '').includes('有数据'),
      `后端应保存验收标准，实际：${derived.body.criteria}`)

    await page.goto(`${base}/project/${pid}?tab=tasks`)
    // 等标准文本出现，而不是读到就断言（避免渲染时序造成的偶发失败）
    const card = page.locator('.task-card', { hasText: '0/2 项标准' }).first()
    await card.waitFor({ timeout: 20000 })
    pass('任务卡显示验收标准进度')

    // 学生推进到待验收
    for (const label of ['开始任务', '提交验收']) {
      await card.getByRole('button', { name: label, exact: true }).click()
      await page.waitForTimeout(900)
    }
    // 用列来定位，卡片本身不显示状态文字（状态由所在列表达）
    const inColumn = (name) => page.locator('.board-col', { hasText: name })
      .locator('.task-card', { hasText: 'E2E 派生任务' }).first()
    await inColumn('待验收').waitFor({ timeout: 10000 })

    // 待验收期间内容冻结，且可撤回
    await inColumn('待验收').click()
    await page.waitForTimeout(400)
    assert.equal(await page.getByText('编辑任务', { exact: true }).count(), 0, '待验收任务不应能打开编辑器')
    await inColumn('待验收').getByRole('button', { name: '撤回修改', exact: true }).click()
    await inColumn('进行中').waitFor({ timeout: 10000 })
    pass('待验收内容冻结且可撤回修改')
    await inColumn('进行中').getByRole('button', { name: '提交验收', exact: true }).click()
    await inColumn('待验收').waitFor({ timeout: 10000 })

    // ---------------------------------------------------------------- 教师：逐条核对标准后验收
    // 每个身份用独立浏览器上下文：同一 context 的多个页面共享 localStorage，
    // 会在第二个页面登录时把第一个页面的登录态覆盖掉。
    const teacherContext = await context.browser().newContext({ viewport: { width: 1440, height: 1000 } })
    const tpage = await teacherContext.newPage()
    pageErrors.push(...watch(tpage))
    await signIn(tpage, 'teacher')
    await tpage.goto(`${base}/project/${pid}?tab=tasks`)
    const verifyCard = tpage.locator('.board-col', { hasText: '待验收' })
      .locator('.task-card', { hasText: 'E2E 派生任务' }).first()
    await verifyCard.waitFor({ timeout: 15000 })
    await verifyCard.getByRole('button', { name: '验收通过', exact: true }).click()
    const verifyModal = tpage.locator('.n-modal', { hasText: '逐条核对标准' })
    await verifyModal.waitFor({ timeout: 10000 })
    await verifyModal.locator('.n-checkbox', { hasText: '有数据' }).click()
    await verifyModal.getByRole('button', { name: '验收通过', exact: true }).click()
    await tpage.getByText('已验收', { exact: false }).first().waitFor({ timeout: 10000 })
    await tpage.waitForTimeout(800)
    const doneCard = tpage.locator('.board-col', { hasText: '已完成' })
      .locator('.task-card', { hasText: 'E2E 派生任务' }).first()
    await doneCard.waitFor({ timeout: 10000 })
    const doneText = await doneCard.innerText()
    assert.ok(doneText.includes('1/2 项标准'), `验收后应显示 1/2 达标，实际：${doneText}`)
    assert.ok(doneText.includes('王老师'), '卡片应显示验收人')
    pass('教师逐条核对标准并验收，卡片显示达标情况与验收人')

    // 批注上出现验收回执
    await tpage.goto(`${base}/project/${pid}?tab=annotations`)
    await tpage.getByText('已转为任务：').first().waitFor({ timeout: 10000 })
    const timeline = await tpage.locator('.n-timeline-item').first().innerText()
    assert.ok(timeline.includes('E2E 派生任务') && timeline.includes('已完成'),
      `批注应回链任务及其状态：${timeline}`)
    pass('批注显示派生任务与验收结果')

    // ---------------------------------------------------------------- 过程评价：趋势与预警回应
    await page.goto(`${base}/project/${pid}?tab=assessment`)
    // 等趋势区（提示或对比）真正渲染出来，而不是只等标题
    const trendHint = page.getByText('今天首次查看', { exact: false })
    await trendHint.waitFor({ timeout: 15000 })
    pass('过程评价不编造趋势（首次查看有说明）')

    // 种子项目 p1 有预警，可回应
    await page.goto(`${base}/project/p1?tab=assessment`)
    await page.locator('.n-tabs-tab', { hasText: '风险预警' }).click()
    const risk = page.locator('article.risk').first()
    // 页签内容懒渲染且有入场动画：等输入框真正可见再交互
    await risk.locator('input').first().waitFor({ state: 'visible', timeout: 15000 })
    const note = `E2E 说明-${Date.now()}`
    await risk.locator('input').first().fill(note)
    await risk.getByRole('button', { name: '回应', exact: true }).click()
    const entry = risk.locator('.response').filter({ hasText: note }).first()
    await entry.waitFor({ timeout: 10000 })
    assert.ok((await entry.innerText()).includes('张三'), '补充说明应带作者')
    pass('成员可对预警补充说明并显示作者')

    // ---------------------------------------------------------------- 离线打卡
    await page.goto(`${base}/project/p1?tab=checkins`)
    await page.getByRole('button', { name: '打卡', exact: true }).waitFor({ timeout: 10000 })
    await page.route('**/api/projects/p1/checkins', (route) =>
      route.request().method() === 'POST' ? route.abort('internetdisconnected') : route.continue())
    const offlineText = `离线打卡-${Date.now()}`
    await page.locator('textarea').first().fill(offlineText)
    await page.getByRole('button', { name: '打卡', exact: true }).click()
    await page.getByText('已保存在本机', { exact: false }).waitFor({ timeout: 10000 })
    await page.getByText('待同步', { exact: false }).first().waitFor({ timeout: 10000 })
    pass('断网时打卡存入本地并显示待同步')

    await page.unroute('**/api/projects/p1/checkins')
    await page.getByRole('button', { name: '立即补交', exact: true }).click()
    await page.getByText('已补交', { exact: false }).waitFor({ timeout: 10000 })
    const checkins = await api('/api/projects/p1/checkins', { token: student.token })
    assert.equal(checkins.body.items.filter((c) => c.content === offlineText).length, 1,
      '补交后应恰好入库一次')
    pass('恢复网络后自动补交，且不会重复入库')

    // ---------------------------------------------------------------- 错题本与间隔复习
    const round = await api('/api/quiz/questions?count=3', { token: student.token })
    assert.equal(round.status, 200)
    for (const q of round.body.items) {
      // 全部答错，确保进入错题本
      await api(`/api/quiz/rounds/${round.body.roundId}/answers`, {
        method: 'POST', token: student.token,
        body: { questionId: q.id, choice: (q.options.length - 1) === 0 ? 1 : 0 },
      })
    }
    await api('/api/quiz/attempts', { method: 'POST', token: student.token, body: { roundId: round.body.roundId } })
    await page.goto(`${base}/quiz`)
    await page.getByRole('button', { name: /复习错题/, exact: false }).waitFor({ timeout: 15000 })
    pass('错题本在服务端保存，刷新后仍可复习')

    await page.getByRole('button', { name: /复习错题/, exact: false }).click()
    const wrongCard = page.locator('.n-card', { hasText: '错题复习' })
    await wrongCard.waitFor({ timeout: 10000 })
    // 复习卡是独立卡片（不再嵌在「开始挑战」卡片里），因此这里的目标是唯一的
    await wrongCard.locator('button').first().click()
    // 必须等到"判定与解析"出现：只等按钮可见是不够的——未作答时"下一题"本就是禁用的可见状态
    const feedback = wrongCard.locator('.n-alert')
    await feedback.waitFor({ timeout: 10000 })
    const feedbackText = await feedback.innerText()
    assert.ok(/答对|答错/.test(feedbackText), `应给出判定与解析，实际：${feedbackText}`)
    const nextButton = wrongCard.getByRole('button', { name: /下一题|完成复习/ })
    await nextButton.waitFor({ timeout: 10000 })
    assert.equal(await nextButton.isEnabled(), true, '答题后"下一题"应可用')
    pass('错题复习可答题并给出判定与解析')

    // ---------------------------------------------------------------- 管理端
    const adminContext = await context.browser().newContext({ viewport: { width: 1440, height: 1000 } })
    const adminPage = await adminContext.newPage()
    pageErrors.push(...watch(adminPage))
    await signIn(adminPage, 'schooladmin')
    await adminPage.goto(`${base}/admin/users`)
    await adminPage.getByText('用户管理').first().waitFor({ timeout: 15000 })
    await adminPage.locator('.n-tabs-tab', { hasText: '操作审计' }).click()
    await adminPage.getByText('管理操作审计').first().waitFor({ timeout: 10000 })
    pass('管理端可查看操作审计')

    const schools = await api('/api/admin/schools', { token: (await login('schooladmin')).token })
    assert.equal(schools.status, 200)
    assert.ok(schools.body.items.length > 0, '应存在学校数据')
    pass('学校归属可用')

    // ---------------------------------------------------------------- 教师端与白板回归
    await tpage.goto(`${base}/teacher`)
    await tpage.getByText('团队总览').first().waitFor({ timeout: 15000 })
    await tpage.goto(`${base}/groups`)
    await tpage.getByText('题库管理').first().waitFor({ timeout: 15000 })
    pass('教师端页面回归：/teacher /groups')

    await page.goto(`${base}/focus`)
    const canvas = page.locator('canvas[aria-label="科创白板"]')
    await canvas.waitFor({ timeout: 15000 })
    await page.getByText('本机已保存', { exact: true }).waitFor({ timeout: 10000 })
    const pixels = () => canvas.evaluate((c) => {
      const data = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
      let count = 0
      for (let i = 3; i < data.length; i += 4) if (data[i]) count += 1
      return count
    })
    assert.equal(await pixels(), 0, '初始白板应为空')
    const rect = await canvas.boundingBox()
    await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.3)
    await page.mouse.down()
    await page.mouse.move(rect.x + rect.width * 0.7, rect.y + rect.height * 0.6, { steps: 20 })
    await page.mouse.up()
    const drawn = await pixels()
    assert.ok(drawn > 1000, '应画出笔迹')
    const snapshot = await canvas.evaluate((c) => c.toDataURL())
    await page.reload()
    await page.getByText('本机已保存', { exact: true }).waitFor({ timeout: 10000 })
    assert.equal(await canvas.evaluate((c) => c.toDataURL()), snapshot, '刷新后应恢复笔迹')
    pass('白板按账号保存并在刷新后恢复')

    // 另一账号应看到自己的空画布
    const other = await login('student2')
    await switchUser(page, other)
    await page.getByText('本机已保存', { exact: true }).waitFor({ timeout: 10000 })
    assert.equal(await pixels(), 0, '其他账号不应看到别人的白板')
    await switchUser(page, student)
    await page.getByText('本机已保存', { exact: true }).waitFor({ timeout: 10000 })
    assert.equal(await pixels(), drawn, '切回后应恢复自己的笔迹')
    pass('白板按账号隔离')

    // ---------------------------------------------------------------- 通知中心
    const unreadBefore = await page.locator('button[aria-label="通知"]').count()
    assert.equal(unreadBefore, 1, '头部应有通知入口')
    await page.locator('button[aria-label="通知"]').click()
    await page.locator('.notify-panel').waitFor({ timeout: 10000 })
    const notifyCount = await page.locator('.notify-item').count()
    assert.ok(notifyCount > 0, '通知面板应有条目')
    await page.getByRole('button', { name: '全部标为已读', exact: true }).click()
    await page.waitForTimeout(800)
    assert.equal(await page.getByRole('button', { name: '全部标为已读', exact: true }).count(), 0,
      '标记后"全部标为已读"应消失')
    pass(`通知中心可展开并标记已读（${notifyCount} 条）`)

    // ---------------------------------------------------------------- 专注关联任务
    const focusTask = await api(`/api/projects/${pid}/tasks`, {
      method: 'POST', token: student.token, body: { title: 'E2E 专注任务' },
    })
    assert.equal(focusTask.status, 201, JSON.stringify(focusTask.body))
    await api(`/api/tasks/${focusTask.body.id}`, {
      method: 'PATCH', token: student.token, body: { assigneeId: 'u1' },
    })
    await page.goto(`${base}/project/${pid}?tab=tasks`)
    const focusCard = page.locator('.board-col', { hasText: '待认领' })
      .locator('.task-card', { hasText: 'E2E 专注任务' }).first()
    await focusCard.waitFor({ timeout: 15000 })
    await focusCard.getByRole('button', { name: '专注', exact: true }).click()
    await page.getByText('正在为「E2E 专注任务」专注', { exact: false }).waitFor({ timeout: 10000 })
    pass('任务卡可一键把番茄钟关联到该任务')

    await page.evaluate(() => {
      const store = document.querySelector('#app').__vue_app__.config.globalProperties.$pinia._s.get('pomodoro')
      store.stopTimer(); store.running = false; store.mode = 'focus'; store.remainSec = 1; store.start()
    })
    await page.waitForTimeout(4000)
    await page.goto(`${base}/project/${pid}?tab=tasks`)
    const focusedCard = page.locator('.task-card', { hasText: 'E2E 专注任务' }).first()
    await focusedCard.waitFor({ timeout: 15000 })
    assert.ok((await focusedCard.innerText()).includes('专注 25 分钟'),
      `任务卡应显示关联专注时长：${await focusedCard.innerText()}`)
    pass('专注时长记到关联任务上并显示在卡片')

    // ---------------------------------------------------------------- 同伴互评
    // 互评的对象必须是队友的成果（不能评自己），因此造一项由李四提交的任务
    const peer = await login('student2')
    const joined = await api(`/api/projects/${pid}/join`, { method: 'POST', token: peer.token })
    assert.equal(joined.status, 201, JSON.stringify(joined.body))
    const peerTask = await api(`/api/projects/${pid}/tasks`, {
      method: 'POST', token: student.token, body: { title: 'E2E 队友任务' },
    })
    for (const body of [{ assigneeId: 'u2' }, { status: 'doing' }, { status: 'review' }]) {
      const res = await api(`/api/tasks/${peerTask.body.id}`, { method: 'PATCH', token: peer.token, body })
      assert.equal(res.status, 200, JSON.stringify(res.body))
    }
    await page.goto(`${base}/project/${pid}?tab=tasks`)
    const reviewCard = page.locator('.board-col', { hasText: '待验收' })
      .locator('.task-card', { hasText: 'E2E 队友任务' }).first()
    await reviewCard.waitFor({ timeout: 15000 })
    await reviewCard.getByRole('button', { name: '互评', exact: true }).click()
    const reviewModal = page.locator('.n-modal', { hasText: '同伴互评' })
    await reviewModal.waitFor({ timeout: 10000 })
    await reviewModal.locator('.n-radio-button', { hasText: '提出疑问' }).click()
    await reviewModal.locator('textarea').fill('E2E：数据来源需要标注')
    await reviewModal.getByRole('button', { name: '提交互评', exact: true }).click()
    await page.locator('.n-timeline-item', { hasText: '提出疑问' }).first().waitFor({ timeout: 10000 })
    assert.ok((await reviewModal.innerText()).includes('数据来源需要标注'), '互评内容应出现在时间线')
    await reviewModal.getByRole('button', { name: '关闭', exact: true }).click()
    pass('同伴互评可提交并显示在时间线')

    // ---------------------------------------------------------------- 教师量化评分与教学总览
    await tpage.goto(`${base}/project/${pid}?tab=assessment`)
    await tpage.getByRole('button', { name: '评分', exact: true }).first().waitFor({ timeout: 15000 })
    await tpage.getByRole('button', { name: '评分', exact: true }).first().click()
    const evalModal = tpage.locator('.n-modal', { hasText: '教师评分' })
    await evalModal.waitFor({ timeout: 10000 })
    assert.equal(await evalModal.locator('.n-input-number').count(), 4, '应有四个评分维度')
    await evalModal.locator('.n-input-number input').first().fill('5')
    await evalModal.getByRole('button', { name: '保存评分', exact: true }).click()
    await tpage.getByText('评分已保存', { exact: false }).first().waitFor({ timeout: 10000 })
    await tpage.waitForTimeout(800)
    const memberTable = await tpage.locator('table').first().innerText()
    assert.ok(/\d+\s*\/\s*20/.test(memberTable), `成员表应显示"总分 / 20"，实际：${memberTable.slice(0, 200)}`)
    pass('教师可按四个维度评分并保存')

    await tpage.goto(`${base}/teacher`)
    await tpage.getByText('教学总览').first().waitFor({ timeout: 15000 })
    await tpage.getByRole('button', { name: '展开', exact: true }).click()
    await tpage.locator('table tbody tr').first().waitFor({ timeout: 10000 })
    assert.ok(await tpage.locator('table tbody tr').count() > 0, '总览应列出项目')
    pass('教学总览可展开并列出项目风险')

    // ---------------------------------------------------------------- 令牌失效
    await page.evaluate(() => localStorage.setItem('innoark_token', 'expired-token'))
    await page.goto(base + '/')
    await page.waitForURL((url) => url.pathname.includes('login'), { timeout: 15000 })
    const leftovers = await page.evaluate(() => ({
      token: localStorage.getItem('innoark_token'), user: localStorage.getItem('innoark_user'),
    }))
    assert.equal(leftovers.token, null, '失效令牌应被清理')
    assert.equal(leftovers.user, null, '本地用户缓存应被清理')
    pass('令牌失效后跳回登录页并清空登录态')

    assert.deepEqual(pageErrors, [], '页面出现脚本错误：' + pageErrors.join(' | '))
    console.log(`\n端到端通过（${results.length} 项）：`)
    for (const line of results) console.log('  ✓ ' + line)
    console.log('\n无页面脚本错误。')
  } catch (err) {
    console.error('\n端到端失败：', err.message)
    console.error('已完成：', results.length ? results.join(' / ') : '（无）')
    if (page) {
      // 失败现场：地址、页面文本、第一个任务卡的结构，用于判断是后端没给数据还是前端没渲染
      console.error('URL:', page.url())
      const body = await page.locator('body').innerText().catch(() => '')
      console.error('页面文本（前 400 字）:', body.slice(0, 400).split(String.fromCharCode(10)).join(' | '))
      const cardHtml = await page.locator('.task-card').first().innerHTML().catch(() => '（没有任务卡）')
      console.error('第一个任务卡的 HTML（前 500 字）:', String(cardHtml).slice(0, 500))
      if (results.length === 0 && !body.includes('项标准')) {
        console.error('提示：页面完全没有"项标准"，说明前端构建产物可能落后于后端契约'
          + '（两个仓库的推送有先后时会出现：CI 检出前端 main 的时刻早于前端推送）。')
      }
      await page.screenshot({ path: 'e2e-failure.png', fullPage: true }).catch(() => {})
      console.error('已保存失败截图：e2e-failure.png')
    }
    process.exitCode = 1
  } finally {
    await browser.close()
  }
})()
