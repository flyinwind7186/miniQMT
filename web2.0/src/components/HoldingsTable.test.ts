import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import HoldingsTable from './HoldingsTable.vue'
import { usePositionsStore } from '../stores/positions'

vi.mock('../composables/useAdviceTooltip', () => ({
  useAdviceTooltip: () => ({ show: vi.fn(), hide: vi.fn() }),
}))

describe('持仓受限状态展示', () => {
  beforeEach(() => { setActivePinia(createPinia()) })

  it('桌面与移动布局仅显示可用数量，受限原因和核对时间放在悬浮提示中', async () => {
    const store = usePositionsStore()
    store.positions = [{
      stock_code: '000001', volume: 1500, available: 0,
      unavailable_volume: 1500, restriction_reason: 't1',
      restriction_detail: '当日买入，T+1 暂不可卖', position_asof: '2026-10-19T09:40:00',
    } as any]
    const wrapper = mount(HoldingsTable)
    const availability = wrapper.findAll('[title]').filter(el => el.attributes('title')?.startsWith('不可卖 '))
    expect(availability).toHaveLength(2)
    for (const el of availability) {
      expect(el.text()).toBe('0')
      expect(el.attributes('title')).toBe('不可卖 1500 · 当日买入，T+1 暂不可卖\n持仓核对时间：2026-10-19 09:40:00')
    }
    expect(wrapper.text()).not.toContain('不可卖')
    expect(wrapper.text()).not.toContain('T+1')
    expect(wrapper.find('tbody tr').findAll('td')[11].text()).toBe('0')

    store.positions[0].restriction_detail = '未完成卖单占用'
    store.positions[0].restriction_reason = 'sell_order'
    await nextTick()
    for (const el of availability) {
      expect(el.attributes('title')).toContain('未完成卖单占用')
      expect(el.attributes('title')).not.toContain('T+1')
    }
    expect(wrapper.text()).not.toContain('未完成卖单占用')
    wrapper.unmount()
  })

  it('旧版响应缺少原因字段时仅在悬浮提示中显示待确认，不从数量差额猜测 T+1', () => {
    usePositionsStore().positions = [{ stock_code: '000001', volume: 1000, available: 0 } as any]
    const wrapper = mount(HoldingsTable)
    const availability = wrapper.findAll('[title]').filter(el => el.attributes('title')?.startsWith('不可卖 '))
    expect(availability).toHaveLength(2)
    for (const el of availability) {
      expect(el.text()).toBe('0')
      expect(el.attributes('title')).toBe('不可卖 -- · 原因待确认\n持仓核对时间：待确认')
    }
    expect(wrapper.text()).not.toContain('不可卖')
    expect(wrapper.text()).not.toContain('T+1')
    wrapper.unmount()
  })
})
