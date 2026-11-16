<template>
  <section class="page" data-module="monitor">
    <header class="page-head">
      <div>
        <h2>采集监控管理</h2>
        <p class="page-desc">
          维护采集监控指标的阈值版本、判定口径与告警恢复。阈值、检测频率和静默时间随版本一起写入，
          进行中的窗口始终使用启动版本。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记监控指标</button>
        <button class="btn" type="button" @click="exportRows">导出采集监控清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>恢复状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">
            <button v-if="column === '指标编号'" class="link" type="button" @click="openDetail(row)">
              {{ row[column] ?? '—' }}
            </button>
            <span v-else>{{ row[column] ?? '—' }}</span>
          </td>
          <td>
            <span :class="recoveryClass(row)">{{ recoveryText(row) }}</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openDetail(row)">指标详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无采集监控数据，可先登记监控指标</td>
        </tr>
      </tbody>
    </table>

    <section class="panel">
      <header class="panel-head">
        <div>
          <h3>监控异常待处理区</h3>
          <p class="panel-desc">失败窗口集中在这里，恢复告警按窗口幂等，重复提交只记一次，不会重复累计。</p>
        </div>
        <button class="btn ghost" type="button" @click="reloadAbnormal">刷新待处理区</button>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>窗口编号</th>
            <th>指标编号</th>
            <th>关联端点</th>
            <th>启动阈值版本</th>
            <th>实际判定版本</th>
            <th>判定结论</th>
            <th>静默状态</th>
            <th>恢复状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="window in abnormalWindows" :key="window.窗口编号">
            <td>{{ window.窗口编号 }}</td>
            <td>{{ window.指标编号 }}</td>
            <td>{{ window.关联端点 ?? '—' }}</td>
            <td>{{ window.启动阈值版本 }}</td>
            <td>{{ window.实际判定版本 }}</td>
            <td>{{ window.判定结论 ?? '—' }}</td>
            <td>{{ window.静默状态 ?? '—' }}</td>
            <td>{{ window.恢复状态 ?? '未恢复' }}（已恢复 {{ window.恢复次数 ?? 0 }} 次）</td>
            <td class="row-actions">
              <button class="link" type="button" @click="recoverWindow(window)">恢复告警</button>
              <button class="link" type="button" @click="openWindow(window)">指标抽屉</button>
            </td>
          </tr>
          <tr v-if="!abnormalWindows.length">
            <td colspan="9" class="empty-state">监控异常待处理区为空</td>
          </tr>
        </tbody>
      </table>
    </section>

    <aside v-if="drawerOpen" class="detail-drawer" data-testid="monitor-drawer">
      <header class="drawer-head">
        <div>
          <h3>指标详情 · {{ detail?.指标编号 ?? '—' }}</h3>
          <p class="panel-desc">规则详情显示检测阈值、判定口径、标准范围和触发条件，窗口引用同一阈值版本。</p>
        </div>
        <button class="btn ghost" type="button" @click="closeDrawer">关闭</button>
      </header>

      <div v-if="detail" class="drawer-body">
        <dl class="detail-grid">
          <div><dt>关联端点</dt><dd>{{ detail.关联端点 ?? '—' }}</dd></div>
          <div><dt>当前阈值版本</dt><dd>{{ detail.阈值版本 }}</dd></div>
          <div><dt>静默状态</dt><dd>{{ detail.静默状态文本 || detail.静默状态?.状态 }}</dd></div>
          <div><dt>失败窗口</dt><dd>{{ detail.失败窗口数 ?? 0 }}</dd></div>
        </dl>

        <h4>检测阈值</h4>
        <dl class="detail-grid">
          <div><dt>成功率阈值</dt><dd>{{ detail.成功率阈值 ?? '—' }}</dd></div>
          <div><dt>延迟阈值</dt><dd>{{ detail.延迟阈值 ?? '—' }}</dd></div>
          <div><dt>错误率阈值</dt><dd>{{ detail.错误率告警 ?? '—' }}</dd></div>
          <div><dt>检测频率</dt><dd>{{ detail.检测频率 ?? '—' }}</dd></div>
          <div><dt>静默时间</dt><dd>{{ detail.静默分钟 ?? '—' }} 分钟</dd></div>
        </dl>

        <h4>判定口径</h4>
        <p class="detail-text">{{ detail.判定口径 ?? '—' }}</p>
        <h4>标准范围</h4>
        <p class="detail-text">{{ detail.标准范围 ?? '—' }}</p>
        <h4>触发条件</h4>
        <p class="detail-text">{{ detail.触发条件 ?? '—' }}</p>

        <h4>阈值版本</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>版本</th>
              <th>成功率阈值</th>
              <th>延迟阈值</th>
              <th>错误率阈值</th>
              <th>检测频率</th>
              <th>静默时间</th>
              <th>状态</th>
              <th>冲突优先级</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="version in detail.阈值版本列表 ?? []" :key="version.版本">
              <td>{{ version.版本 }}</td>
              <td>{{ version.成功率阈值 }}</td>
              <td>{{ version.延迟阈值 }}</td>
              <td>{{ version.错误率告警 }}</td>
              <td>{{ version.检测频率 }}</td>
              <td>{{ version.静默分钟 }} 分钟</td>
              <td>{{ version.状态 }}</td>
              <td>{{ version.冲突?.优先级 ?? '—' }}</td>
            </tr>
          </tbody>
        </table>

        <h4>阈值窗口</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>窗口编号</th>
              <th>启动版本</th>
              <th>实际判定版本</th>
              <th>判定结论</th>
              <th>状态</th>
              <th>恢复状态</th>
              <th>可执行动作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="window in detail.窗口列表 ?? []" :key="window.窗口编号">
              <td>{{ window.窗口编号 }}</td>
              <td>{{ window.启动阈值版本 }}</td>
              <td>{{ window.实际判定版本 }}</td>
              <td>{{ window.判定结论 ?? '—' }}</td>
              <td>{{ window.status }}</td>
              <td>{{ window.恢复状态 }}（{{ window.恢复次数 ?? 0 }} 次）</td>
              <td class="row-actions">
                <button class="link" type="button" @click="recoverWindow(window)">恢复告警</button>
              </td>
            </tr>
            <tr v-if="!(detail.窗口列表 ?? []).length">
              <td colspan="7" class="empty-state">该指标暂无阈值窗口</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty-state">指标详情读取中…</p>
    </aside>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集监控记录</span>
      <span v-if="message" class="tip-text">{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, any>
type WindowRow = Record<string, any>

const ENDPOINT = '/api/monitor'
const columns = [
  '指标编号',
  '关联端点',
  '成功率阈值',
  '延迟阈值',
  '错误率告警',
  '检测频率',
  '静默分钟',
  '阈值版本',
  '静默状态文本',
]
const actions = ['提交校验', '启用监控', '停用监控']
const statLabels = ['指标总数', '已启用指标', '异常待处理窗口', '待确认阈值版本', '静默中指标']

const rows = ref<Row[]>([])
const total = ref(0)
const abnormalWindows = ref<WindowRow[]>([])
const stats = ref(statLabels.map((label) => ({ label, value: 0 })))
const errorMessage = ref('')
const message = ref('')
const drawerOpen = ref(false)
const detail = ref<Row | null>(null)
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  message.value = '监控指标登记入口尚未接入审批流'
}

function recoveryText(row: Row) {
  if (Number(row.失败窗口数 ?? 0) > 0) {
    return `待恢复（失败窗口 ${row.失败窗口数}）`
  }
  if (Number(row.恢复窗口数 ?? 0) > 0) {
    return `已恢复 ${row.恢复记录数 ?? row.恢复窗口数} 次（重复提交不累计）`
  }
  return '无待恢复告警'
}

function recoveryClass(row: Row) {
  if (Number(row.失败窗口数 ?? 0) > 0) return 'status-warn'
  if (Number(row.恢复窗口数 ?? 0) > 0) return 'status-ok'
  return ''
}

function closeDrawer() {
  drawerOpen.value = false
  detail.value = null
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const payload = await fetchJson<Row>(`${ENDPOINT}/by-code/${encodeURIComponent(String(row.指标编号))}`)
    detail.value = payload
    drawerOpen.value = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标详情读取失败'
  }
}

function openWindow(window: WindowRow) {
  void openDetail({ 指标编号: window.指标编号 })
}

async function reloadAbnormal() {
  try {
    const payload = await fetchJson<{ items: WindowRow[] }>(`${ENDPOINT}/abnormal`)
    abnormalWindows.value = (payload.items ?? []).map((window) => ({
      ...window,
      静默状态: window.静默状态文本 ?? window.静默状态?.状态 ?? '—',
    }))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '监控异常待处理区读取失败'
  }
}

async function recoverWindow(window: WindowRow) {
  errorMessage.value = ''
  message.value = ''
  const version = String(window.启动阈值版本 ?? window.阈值版本 ?? '')
  const confirmed = window.confirm(
    `恢复告警前核对阈值版本：窗口 ${window.窗口编号} 使用 ${version}。确认恢复？`,
  )
  if (!confirmed) return
  try {
    const response = await request(`${ENDPOINT}/windows/${encodeURIComponent(String(window.窗口编号))}/recover`, {
      method: 'POST',
      body: JSON.stringify({ values: { 阈值版本: version, operator: '值班管理员' } }),
    })
    const payload = await response.json()
    message.value = payload.message
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '恢复告警未生效')
    }
    await Promise.all([reload(), reloadAbnormal()])
    if (drawerOpen.value && detail.value) {
      const current = String(detail.value.指标编号)
      await openDetail({ 指标编号: current })
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复告警失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '采集监控动作未生效，请稍后重试')
    }
    message.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集监控操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [listPayload, summaryPayload] = await Promise.all([
      fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?${query}`),
      fetchJson<Record<string, number>>(`${ENDPOINT}/summary`),
    ])
    rows.value = listPayload.items ?? []
    total.value = listPayload.total ?? rows.value.length
    stats.value = statLabels.map((label) => ({ label, value: summaryPayload[label] ?? 0 }))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集监控列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  await reloadAbnormal()
})
</script>
