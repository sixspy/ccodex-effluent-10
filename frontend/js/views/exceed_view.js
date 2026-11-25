export const exceedViewTemplate = `
  <div class="panel">
    <h3>超标旁路挂起 · 闭环同重裁（effluent-10）</h3>
    <p class="muted" style="margin-top:0">
      闭环不是只关挂起行：甲路径须同事务重裁指标并写新裁定，指标仍超标则整笔拒收；
      乙路径只落影子裁定，升正前准排页一律不认。两路抢同一挂起只有一路成功，后到留争用痕。
    </p>
    <table>
      <thead><tr>
        <th>键</th><th>采样</th><th>指标</th><th>测值</th><th>限值</th><th>状态</th>
        <th style="min-width:26rem">闭环重裁（复测值 / 口径 / 拍板说明）</th><th>新裁定时戳</th>
      </tr></thead>
      <tbody>
        <template v-for="h in exceeds" :key="h.id">
          <tr>
            <td>{{ h.hold_key }}</td><td>{{ h.sample_code }}</td><td>{{ h.exceed_metric }}</td>
            <td>{{ h.measured }}</td><td>{{ h.limit_value }}</td>
            <td :class="h.status==='开放'?'fail':'pass'">{{ h.status }}</td>
            <td v-if="h.status==='开放'">
              <div class="close-recalc-grid">
                <label>COD 复测<input type="number" step="0.1" v-model.number="closeDraft(h).cod" :placeholder="h.current_metrics && h.current_metrics.cod != null ? ('旧值 ' + h.current_metrics.cod) : '复测值'"/></label>
                <label>氨氮 复测<input type="number" step="0.01" v-model.number="closeDraft(h).nh3n" :placeholder="h.current_metrics && h.current_metrics.nh3n != null ? ('旧值 ' + h.current_metrics.nh3n) : '复测值'"/></label>
                <label>总磷 复测<input type="number" step="0.001" v-model.number="closeDraft(h).tp" :placeholder="h.current_metrics && h.current_metrics.tp != null ? ('旧值 ' + h.current_metrics.tp) : '复测值'"/></label>
                <label>口径
                  <select v-model="closeDraft(h).lane">
                    <option value="formal">甲·同事务正式重裁</option>
                    <option value="shadow">乙·仅影子裁定</option>
                  </select>
                </label>
              </div>
              <p class="gate-hint" style="margin:.3rem 0">三格复测值可留空：留空按在库指标如实重裁并写新时戳；全填则同事务改写正式指标，若仍超标整笔拒收。</p>
              <input style="margin:.35rem 0" v-model="closeDraft(h).note"
                     placeholder="拍板说明（记入说明，当班锁死）"/>
              <button class="secondary" style="width:auto;margin:0" @click="submitClose(h)">
                闭环并重裁
              </button>
              <span class="muted" style="margin-left:.5rem">旧值：{{ fmtMetrics(h.current_metrics) }}</span>
            </td>
            <td v-else class="muted">已闭环{{ h.closed_at ? ' · ' + h.closed_at : '' }}</td>
            <td>
              <span v-if="h.recalc_at" class="pass">{{ h.recalc_at }}</span>
              <span v-else class="muted">—</span>
              <div v-if="h.adjudication_no" class="muted" style="font-size:.78rem">
                {{ h.adjudication_no }}<template v-if="h.recalc_lane"> · {{ h.recalc_lane }}</template>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>`;
