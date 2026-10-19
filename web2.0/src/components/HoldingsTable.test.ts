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

  it('桌面与移动布局均展示后端确认的受限原因和核对时间', async () => {
    const store = usePositionsStore()
    store.positions = [{
      stock_code: '000001', volume: 1500, available: 0,
      unavailable_volume: 1500, restriction_reason: 't1',
      restriction_detail: '当日买入，T+1 暂不可卖', position_asof: '2026-10-19T09:40:00',
    } as any]
    const wrapper = mount(HoldingsTable)
    expect(wrapper.text().match(/不可卖 1500/g)).toHaveLength(2)
    expect(wrapper.text()).toContain('当日买入，T+1 暂不可卖')
    expect(wrapper.findAll('[title="持仓核对时间：2026-10-19 09:40:00"]')).toHaveLength(2)

    store.positions[0].restriction_detail = '未完成卖单占用'
    store.positions[0].restriction_reason = 'sell_order'
    await nextTick()
    expect(wrapper.text()).toContain('未完成卖单占用')
    expect(wrapper.text()).not.toContain('T+1 暂不可卖')
    wrapper.unmount()
  })

  it('旧版响应缺少原因字段时显示待确认，不从数量差额猜测 T+1', () => {
    usePositionsStore().positions = [{ stock_code: '000001', volume: 1000, available: 0 } as any]
    const wrapper = mount(HoldingsTable)
    expect(wrapper.text()).toContain('不可卖 -- · 原因待确认')
    expect(wrapper.text()).not.toContain('T+1')
    wrapper.unmount()
  })
})
