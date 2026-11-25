export const samplesViewTemplate = `
  <div class="grid-2">
    <div class="panel">
      <h3>采样批次</h3>
      <table>
        <thead><tr><th>编号</th><th>口</th><th>许可</th><th>流量</th><th>状态</th><th>代</th><th>裁定时戳 / 裁定</th></tr></thead>
        <tbody>
          <tr v-for="s in samples" :key="s.id" @click="loadSample(s.id)" style="cursor:pointer">
            <td>{{ s.sample_code }}</td><td>{{ s.outfall_code }}</td><td>{{ s.permit_ref }}</td>
            <td>{{ s.flow_m3h }}</td><td :class="sampleClass(s.status)">{{ s.status }}</td>
            <td>{{ s.generation }}</td>
            <td>
              <span :class="s.adjudicated_at ? 'pass' : 'muted'">{{ s.adjudicated_at || '未裁定' }}</span>
              <div v-if="s.latest_adjudication" style="font-size:.78rem"
                   :class="s.latest_adjudication.verdict==='合格' ? 'pass' : 'fail'">
                {{ s.latest_adjudication.lane }} · {{ s.latest_adjudication.verdict }} · {{ s.latest_adjudication.status }}
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="sampleDetail" class="panel" style="margin-top:0.8rem;border-style:dashed">
        <h3>批次详情 · {{ sampleDetail.sample.sample_code }}</h3>
        <div class="kv">
          <span>指标</span><span>{{ fmtMetrics(sampleDetail.sample.metrics) }}</span>
          <span>裁定时戳</span>
          <span :class="sampleDetail.sample.adjudicated_at ? 'pass' : 'muted'">
            {{ sampleDetail.sample.adjudicated_at || '无（影子未升正/未重裁）' }}
          </span>
          <span>挂起</span><span>{{ sampleDetail.exceed_holds.length }}</span>
          <span>准排</span><span>{{ sampleDetail.discharge_releases.length }}</span>
          <span>封批</span><span>{{ sampleDetail.discharge_seals.length }}</span>
        </div>
        <h4 style="margin:.8rem 0 .4rem">重裁裁定台账（甲正式 / 乙影子，同源）</h4>
        <table v-if="sampleDetail.adjudications && sampleDetail.adjudications.length">
          <thead><tr><th>#</th><th>口径</th><th>结论</th><th>状态</th><th>重裁指标</th><th>时戳</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="a in sampleDetail.adjudications" :key="a.id">
              <td>{{ a.adjudication_no || a.id }}</td>
              <td>{{ a.lane }}<span class="muted" v-if="a.path_code">（{{ a.path_code }}）</span></td>
              <td :class="a.verdict==='合格' ? 'pass' : 'fail'">{{ a.verdict }}</td>
              <td>{{ a.status }}</td>
              <td class="muted" style="font-size:.78rem">{{ fmtMetrics(a.metrics) }}</td>
              <td>{{ a.adjudicated_at }}</td>
              <td>
                <button v-if="a.lane==='影子' && a.status==='影子待升正'"
                        class="secondary" style="width:auto;margin:0 .25rem 0 0"
                        @click.stop="promoteAdj(a.id)">确认升正</button>
                <button v-if="a.lane==='影子' && a.status==='影子待升正'"
                        class="secondary" style="width:auto;margin:0"
                        @click.stop="voidAdj(a.id)">作废</button>
                <span v-else class="muted">—</span>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">暂无重裁裁定；超标挂起闭环后在此写出新裁定。</p>
      </div>
    </div>
    <div class="panel">
      <h3>入库采样</h3>
      <div class="form-row"><div><label>排放口 ID</label><input type="number" v-model.number="sampleForm.outfall_id"/></div>
      <div><label>许可号</label><input v-model="sampleForm.permit_ref"/></div></div>
      <div class="form-row">
        <div><label>COD</label><input type="number" v-model.number="sampleForm.cod"/></div>
        <div><label>氨氮</label><input type="number" v-model.number="sampleForm.nh3n"/></div>
      </div>
      <div class="form-row">
        <div><label>总磷</label><input type="number" v-model.number="sampleForm.tp"/></div>
        <div><label>流量 m3/h</label><input type="number" v-model.number="sampleForm.flow_m3h"/></div>
      </div>
      <label>编号（可空自动）</label><input v-model="sampleForm.sample_code"/>
      <div class="actions"><button @click="createSample">入库</button></div>
      <p class="muted">入库即按排放口限值裁定：合格直接“已裁定”，超标自动开 EX 挂起。</p>
    </div>
  </div>`;
