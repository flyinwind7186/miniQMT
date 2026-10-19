<script setup lang="ts">
import { computed, ref } from 'vue'
import { usePositionsStore } from '../stores/positions'
import { useGridStore } from '../stores/grid'
import { fmtPrice, fmtPercent, fmtMoney } from '../utils/format'
import { formatAge } from '../utils/freshness'
import { useAdviceTooltip } from '../composables/useAdviceTooltip'
import GridHoverCard from './GridHoverCard.vue'

/** 持仓只读列表。无勾选、无网格启停入口——所有操作在 web1.0 完成。 */
const { show: showAdvice, hide: hideAdvice } = useAdviceTooltip()

const positions = usePositionsStore()
const grid = useGridStore()
const sortKey = ref<string>('profit_ratio')
const sortDir = ref<-1 | 1>(-1)

// 网格悬停卡状态
const hoverCode = ref('')
const hoverAnchor = ref<{ x: number; y: number } | null>(null)

const sorted = computed(() => {
  const arr = [...positions.positions]
  arr.sort((a: any, b: any) => {
    const va = a[sortKey.value] ?? 0; const vb = b[sortKey.value] ?? 0
    return (va > vb ? 1 : va < vb ? -1 : 0) * sortDir.value
  })
  return arr
})

function toggleSort(key: string) {
  if (sortKey.value === key) sortDir.value = (sortDir.value * -1) as -1 | 1
  else { sortKey.value = key; sortDir.value = -1 }
}

function openAdvice(event: Event, code: string) { showAdvice(event as MouseEvent, code) }

function hasActiveGrid(pos: any): boolean {
  return pos.grid_session_active === true || grid.isActiveForStock(pos.stock_code)
}

function showGridCard(event: MouseEvent, code: string) {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  hoverAnchor.value = { x: rect.left, y: rect.bottom + 6 }
  hoverCode.value = code
}
function hideGridCard() { hoverCode.value = ''; hoverAnchor.value = null }

const COLS = [
  { k: 'stock_code',        l: '代码',   s: true,  c: 'tabular-nums' },
  { k: 'stock_name',        l: '名称',   s: false, c: 'text-slate-500 max-w-[96px]' },
  { k: 'change_percentage', l: '涨跌幅', s: true,  c: 'text-right tabular-nums' },
  { k: 'current_price',     l: '现价',   s: true,  c: 'text-right tabular-nums' },
  { k: 'cost_price',        l: '成本',   s: true,  c: 'text-right tabular-nums text-slate-500' },
  { k: 'base_cost_price',   l: '基准',   s: true,  c: 'text-right tabular-nums text-slate-400' },
  { k: 'profit_ratio',      l: '盈亏',   s: true,  c: 'text-right tabular-nums font-semibold' },
  { k: 'profit_amount',     l: '浮盈',   s: true,  c: 'text-right tabular-nums' },
  { k: 'market_value',      l: '市值',   s: true,  c: 'text-right tabular-nums' },
  { k: 'volume',            l: '持仓',   s: true,  c: 'text-right tabular-nums' },
  { k: 'available',         l: '可用',   s: true,  c: 'text-right tabular-nums text-slate-500' },
  { k: 'profit_triggered',  l: '止盈',   s: false, c: 'text-center' },
  { k: 'highest_price',     l: '最高',   s: false, c: 'text-right tabular-nums' },
  { k: 'stop_loss_price',   l: '止损',   s: false, c: 'text-right tabular-nums text-slate-500' },
  { k: 'open_date',         l: '建仓',   s: true,  c: 'text-slate-500 whitespace-nowrap' },
  { k: 'stop_profit_enabled', l: '自动止盈', s: false, c: 'text-center' },
]

/** 浮动盈亏金额：后端未给时按 (现价-成本)×数量 兜底 */
function profitAmount(pos: any): number {
  if (typeof pos.profit_amount === 'number') return pos.profit_amount
  return ((pos.current_price || 0) - (pos.cost_price || 0)) * (pos.volume || 0)
}

function cellValue(pos: any, col: typeof COLS[0]): string {
  const v = pos[col.k]
  if (col.k === 'profit_ratio' || col.k === 'change_percentage') return fmtPercent(v)
  if (col.k === 'profit_amount') return fmtMoney(profitAmount(pos), 0)
  if (['current_price', 'cost_price', 'base_cost_price', 'highest_price', 'stop_loss_price'].includes(col.k)) return fmtPrice(v)
  if (col.k === 'market_value') return fmtMoney(v, 0)
  if (col.k === 'open_date') return (v || '').substring(0, 10) || '--'
  return v ?? '--'
}

function profitBg(v: number): string {
  // A股习惯：红涨绿跌
  if (v > 0) return 'bg-red-50/40'
  if (v < 0) return 'bg-emerald-50/40'
  return ''
}

function shortName(pos: any): string {
  return pos.stock_name || pos.stock_code
}

function availabilityTitle(pos: any): string {
  return `不可卖 ${pos.unavailable_volume ?? '--'} · ${pos.restriction_detail || '原因待确认'}\n持仓核对时间：${pos.position_asof?.replace('T', ' ') || '待确认'}`
}

const age = computed(() => formatAge(positions.positionsUpdatedAt))
</script>

<template>
  <div class="card">
    <div class="card-header">
      <div class="flex items-center gap-2">
        <svg class="w-4 h-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 10h16M4 14h16M4 18h16"/></svg>
        <span>持仓列表</span>
        <span class="badge-blue text-[10px]">{{ positions.positions.length }} 只</span>
      </div>
      <span class="text-xs text-slate-400">
        总市值 <strong class="text-slate-700">{{ fmtMoney(positions.totalMarketValue) }}</strong>
        <span class="ml-2 font-mono text-[10px]">{{ age }}</span>
      </span>
    </div>

    <div class="md:hidden p-3 space-y-2">
      <div v-if="sorted.length === 0" class="py-12 text-center">
        <svg class="w-12 h-12 text-slate-200 mx-auto mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"/></svg>
        <p class="text-slate-400 font-medium">暂无持仓数据</p>
      </div>

      <article v-for="pos in sorted" :key="pos.stock_code" class="mobile-card">
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0 flex-1">
            <span class="font-mono text-sm font-bold text-slate-700">{{ pos.stock_code }}</span>
            <div class="mt-0.5 flex min-w-0 items-center gap-1.5">
              <span class="min-w-0 truncate text-xs text-slate-500 cursor-help" @mouseenter="openAdvice($event, pos.stock_code)" @mouseleave="hideAdvice()">{{ shortName(pos) }}</span>
              <button
                type="button"
                class="inline-flex h-5 flex-shrink-0 items-center rounded-full border border-amber-200 bg-amber-50 px-1.5 text-[10px] font-semibold leading-none text-amber-700 hover:bg-amber-100 focus:outline-none focus:ring-2 focus:ring-amber-300"
                title="MACD 操作建议"
                @mouseenter="openAdvice($event, pos.stock_code)"
                @mouseleave="hideAdvice()"
                @focus="openAdvice($event, pos.stock_code)"
                @blur="hideAdvice()"
                @click.stop="openAdvice($event, pos.stock_code)"
              >建议</button>
              <span v-if="hasActiveGrid(pos)" class="badge-green !text-[9px] !px-1.5 !py-0">网格</span>
              <span v-if="pos.profit_triggered" class="badge-amber !text-[9px] !px-1.5 !py-0">止盈</span>
              <span v-if="pos.stop_profit_enabled === false" class="badge-slate !text-[9px] !px-1.5 !py-0" title="该股动态止盈止损已暂停">止盈关</span>
            </div>
          </div>
          <div class="flex-shrink-0 text-right">
            <div :class="['font-mono text-base font-bold tabular-nums', (pos.profit_ratio ?? 0) > 0 ? 'text-red-600' : (pos.profit_ratio ?? 0) < 0 ? 'text-emerald-600' : 'text-slate-500']">
              {{ fmtPercent(pos.profit_ratio || 0) }}
            </div>
            <div :class="['mt-0.5 font-mono text-xs tabular-nums', (pos.change_percentage ?? 0) > 0 ? 'text-red-600' : (pos.change_percentage ?? 0) < 0 ? 'text-emerald-600' : 'text-slate-500']">
              今日 {{ fmtPercent(pos.change_percentage || 0) }}
            </div>
          </div>
        </div>

        <div class="mt-3 grid grid-cols-3 gap-2">
          <div class="rounded-md bg-slate-50 px-2 py-1.5">
            <div class="text-[10px] text-slate-400">现价</div>
            <div class="truncate font-mono text-sm text-slate-700">{{ fmtPrice(pos.current_price) }}</div>
          </div>
          <div class="rounded-md bg-slate-50 px-2 py-1.5">
            <div class="text-[10px] text-slate-400">成本</div>
            <div class="truncate font-mono text-sm text-slate-700">{{ fmtPrice(pos.cost_price) }}</div>
          </div>
          <div class="rounded-md bg-slate-50 px-2 py-1.5">
            <div class="text-[10px] text-slate-400">浮盈</div>
            <div class="truncate font-mono text-sm text-slate-700">{{ fmtMoney(profitAmount(pos), 0) }}</div>
          </div>
        </div>

        <div class="mt-3 flex items-center justify-between gap-2 text-xs text-slate-500">
          <span>持仓 <strong class="font-mono text-slate-700">{{ pos.volume }}</strong></span>
          <span>可用 <strong class="font-mono text-slate-700" :title="availabilityTitle(pos)">{{ pos.available }}</strong></span>
          <span>市值 <strong class="font-mono text-slate-700">{{ fmtMoney(pos.market_value, 0) }}</strong></span>
        </div>
      </article>
    </div>

    <div class="hidden md:block overflow-x-auto -mx-3 md:mx-0">
      <div class="min-w-[980px] md:min-w-0">
        <table class="w-full text-xs">
        <thead>
          <tr class="bg-slate-50/80 border-b border-slate-200">
            <th class="pl-5 pr-2 py-2.5 w-10 text-[10px] font-semibold text-slate-400">网格</th>
            <th v-for="col in COLS" :key="col.k"
              :class="['px-2 py-2.5 font-semibold text-slate-500 whitespace-nowrap select-none', col.c,
                       col.s ? 'cursor-pointer hover:text-slate-800' : '']"
              @click="col.s && toggleSort(col.k)">
              <span class="inline-flex items-center gap-0.5">
                {{ col.l }}
                <span v-if="sortKey === col.k" class="text-blue-500 text-[10px]">{{ sortDir === -1 ? '▼' : '▲' }}</span>
              </span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="sorted.length === 0">
            <td :colspan="COLS.length + 1" class="py-16 text-center">
              <div class="flex flex-col items-center gap-3">
                <svg class="w-12 h-12 text-slate-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"/></svg>
                <p class="text-slate-400 font-medium">暂无持仓数据</p>
                <p class="text-slate-300 text-[11px]">等待 QMT 同步持仓信息...</p>
              </div>
            </td>
          </tr>
          <tr v-for="pos in sorted" :key="pos.stock_code"
            :class="['border-b border-slate-100 hover:bg-slate-50/70 transition-colors group', profitBg(pos.profit_ratio)]">
            <!-- 网格状态指示（悬停查看详情，无操作入口） -->
            <td class="pl-5 pr-2 py-2 text-center">
              <span v-if="hasActiveGrid(pos)"
                class="inline-block w-2.5 h-2.5 rounded-full bg-emerald-500 cursor-help"
                title="网格运行中 · 悬停查看详情"
                @mouseenter="showGridCard($event, pos.stock_code)"
                @mouseleave="hideGridCard()"></span>
              <span v-else class="inline-block w-2.5 h-2.5 rounded-full bg-slate-200" title="无网格会话"></span>
            </td>
            <td class="px-2 py-2 font-semibold font-mono text-slate-700">{{ pos.stock_code }}</td>
            <td v-for="col in COLS.slice(1)" :key="col.k"
              :class="['px-2 py-2 whitespace-nowrap', col.c, col.k === 'stock_name' ? 'cursor-help' : '',
                       (col.k === 'profit_ratio' || col.k === 'change_percentage' || col.k === 'profit_amount')
                         ? (((col.k === 'profit_amount' ? profitAmount(pos) : pos[col.k]) ?? 0) > 0 ? 'text-red-600'
                            : ((col.k === 'profit_amount' ? profitAmount(pos) : pos[col.k]) ?? 0) < 0 ? 'text-emerald-600' : 'text-slate-400')
                         : '']"
              @mouseenter="col.k === 'stock_name' && openAdvice($event, pos.stock_code)"
              @mouseleave="col.k === 'stock_name' && hideAdvice()">
              <span v-if="col.k === 'profit_triggered'" class="inline-flex items-center justify-center">
                <svg v-if="pos.profit_triggered" class="w-4 h-4 text-emerald-500" fill="none" stroke="currentColor" viewBox="0 0 24 24" title="已触发首次止盈"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/></svg>
                <span v-else class="text-slate-300" title="未触发">—</span>
              </span>
              <span v-else-if="col.k === 'stop_profit_enabled'">
                <span v-if="pos.stop_profit_enabled === false" class="badge-slate !text-[9px] !px-1.5" title="该股动态止盈止损已暂停（在 web1.0 中切换）">关</span>
                <span v-else-if="pos.stop_profit_enabled === true" class="badge-green !text-[9px] !px-1.5" title="该股动态止盈止损已开启">开</span>
                <span v-else class="text-slate-300" title="后端未提供该字段">--</span>
              </span>
              <span v-else-if="col.k === 'available'" :title="availabilityTitle(pos)">
                {{ cellValue(pos, col) }}
              </span>
              <span v-else-if="col.k === 'stock_name'" class="inline-flex min-w-0 max-w-full items-center gap-1.5">
                <span class="min-w-0 truncate">{{ cellValue(pos, col) }}</span>
                <button
                  type="button"
                  class="inline-flex h-5 flex-shrink-0 items-center rounded-full border border-amber-200 bg-amber-50 px-1.5 text-[10px] font-semibold leading-none text-amber-700 hover:bg-amber-100 focus:outline-none focus:ring-2 focus:ring-amber-300"
                  title="MACD 操作建议"
                  @mouseenter="openAdvice($event, pos.stock_code)"
                  @mouseleave="hideAdvice()"
                  @focus="openAdvice($event, pos.stock_code)"
                  @blur="hideAdvice()"
                  @click.stop="openAdvice($event, pos.stock_code)"
                >建议</button>
              </span>
              <span v-else>{{ cellValue(pos, col) }}</span>
            </td>
          </tr>
        </tbody>
      </table>
      </div>
    </div>

    <GridHoverCard v-if="hoverCode && hoverAnchor" :stock-code="hoverCode" :anchor="hoverAnchor" />
  </div>
</template>
