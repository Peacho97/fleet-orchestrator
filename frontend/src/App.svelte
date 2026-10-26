<script>
  // Control room for a local AI fleet. Talks to its own backend, which aggregates
  // GPU telemetry, the GPU lease broker and the session broker. Each panel
  // degrades alone.
  import { onMount, onDestroy } from 'svelte'

  let gpu      = $state(null)
  let sessions = $state(null)
  let tree     = $state(null)
  let activeRun = $state(null)
  let richRuns = $state([])   // per-run summary meta for the Workflows list
  let sources  = $state({ gpu_broker: false, session_broker: false })
  let loading  = $state(true)
  let cfg      = $state({ gpu_broker_url: 'GPU broker', session_broker_url: 'session broker' })
  let poll = null

  const PRELOAD = [
    { name: 'llama3.2:3b',       mb: 2600 },
    { name: 'gemma2:2b',         mb: 1700 },
    { name: 'qwen2.5-coder:7b',  mb: 5400 },
    { name: 'qwen3:14b',         mb: 9000 },
  ]

  const api = (u, opts) => fetch(u, opts).then(r => r.json()).catch(() => null)

  async function loadAll() {
    const [ov, src] = await Promise.all([api('/api/overview'), api('/api/sources')])
    if (ov) { gpu = ov.gpu; sessions = ov.sessions; if (ov.config) cfg = ov.config }
    if (src) sources = src
    const runs = sessions?.runs || []
    const running = runs.find(r => (r.status || '').toLowerCase().includes('run')) || runs[0]
    const rid = running?.id || running?.run_id
    if (rid && rid !== activeRun) activeRun = rid
    if (activeRun) tree = (await api(`/api/sessions/${activeRun}/tree`))?.tree || null
    richRuns = (await api('/api/sessions/rich'))?.runs || richRuns
    loading = false
  }

  async function archiveRun(rid, goal, e) {
    e?.stopPropagation?.()
    if (!confirm(`Archive workflow "${goal || rid}"?\n\nIt moves out of the broker's runs folder (reversible - kept in _archived_runs). It'll disappear from this list.`)) return
    const res = await api(`/api/sessions/${rid}/archive`, { method: 'POST' })
    if (res?.ok) { if (rid === activeRun) { activeRun = null; tree = null } await loadAll() }
    else alert('Archive failed: ' + (res?.error || 'unknown'))
  }

  let loadingModel = $state(null)   // name of the model currently being preloaded
  let loadErr      = $state(null)   // last preload error, shown inline

  async function preloadModel(m, card) {
    const freeGb = card?.free_mb == null ? '?' : (card.free_mb / 1024).toFixed(1)
    const gaming = (card?.state === 'gaming')
    const warn = gaming
      ? `⚠ ${card.name} is GAMING right now (${card.foreign_holder || 'a non-AI app'} holds it). `
        + `Loading ${m.name} will consume ${(m.mb/1024).toFixed(1)}G of its VRAM and may stutter the game.\n\n`
      : ''
    const ok = confirm(`${warn}Load ${m.name} (${(m.mb/1024).toFixed(1)}G) into ${card?.name || 'this card'}'s free VRAM (${freeGb}G free)?`)
    if (!ok) return
    loadErr = null
    loadingModel = m.name
    const res = await api('/api/gpu/preload', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: m.name }) })
    loadingModel = null
    if (!res?.ok) loadErr = `${m.name}: ${res?.error || 'load failed'}`
    await loadAll()
  }

  async function togglePause() {
    const next = !(sessions?.paused)
    await api('/api/sessions/pause', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paused: next }) })
    loadAll()
  }

  // ── tabs ──
  let tab = $state('control')   // 'control' | 'sessions'

  // ── Sessions tab state (drives the session broker) ──
  let tasks     = $state([])
  let selRunId  = $state(null)
  let selTree   = $state(null)
  let selGate   = $state(null)
  let launching = $state(false)
  let launchMsg = $state('')
  let lgGoal    = $state('')
  let lgRepo    = $state('')
  let lgTarget  = $state('')

  async function loadTasks() { tasks = (await api('/api/sessions/tasks'))?.tasks || [] }

  async function refreshSelected() {
    if (!selRunId) { selTree = null; selGate = null; return }
    const [t, g] = await Promise.all([
      api(`/api/sessions/${selRunId}/tree`), api(`/api/sessions/${selRunId}/gate`)])
    selTree = t?.tree || null
    selGate = g?.gate || null
  }
  function selectRun(id) { selRunId = id; refreshSelected() }

  async function launchTask(taskFile) {
    launching = true; launchMsg = ''
    const body = taskFile
      ? { task_file: taskFile }
      : { task_spec: { goal: lgGoal, repo_path: lgRepo, target_file: lgTarget } }
    const res = await api('/api/sessions/launch', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    launching = false
    if (res?.run) { launchMsg = `launched ${res.run}`; lgGoal = ''; lgRepo = ''; lgTarget = ''; await loadAll(); selectRun(res.run) }
    else { launchMsg = res?.error ? `error: ${res.error}` : 'launch failed' }
  }

  async function decideGate(approved) {
    if (!selRunId || !selGate?.pending) return
    await api(`/api/sessions/${selRunId}/gate`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gate: selGate.pending, approved, notes: '' }) })
    await refreshSelected()
  }

  // ── Workflow inspector - click a run/node → popup with info + node transcripts ──
  let inspRun     = $state(null)
  let inspTree    = $state(null)
  let inspDetail  = $state(null)
  let inspNode    = $state(null)
  let inspText    = $state('')
  let inspLoading = $state(false)
  let inspSummaries = $state({})      // node_id -> 2-sentence "what it did"
  let inspSumLoading = $state(false)

  async function openInspector(runId, node = null) {
    if (!runId) return
    if (runId !== inspRun) {
      inspRun = runId; inspTree = null; inspDetail = null; inspNode = null; inspText = ''
      inspSummaries = {}; inspSumLoading = true
      const [t, d] = await Promise.all([
        api(`/api/sessions/${runId}/tree`), api(`/api/sessions/${runId}/detail`)])
      inspTree = t?.tree || null
      inspDetail = d?.run || null
      // local-model summaries land a beat later (first open runs the model)
      api(`/api/sessions/${runId}/summaries`).then(s => {
        inspSummaries = s?.summaries || {}; inspSumLoading = false
      })
    }
    if (node) selectInspNode(node)
  }
  function closeInspector() { inspRun = null; inspNode = null; inspText = '' }
  async function selectInspNode(node) {
    inspNode = node; inspText = ''
    if (!node?.transcript) { inspText = '(this node has no transcript - e.g. the conductor)'; return }
    inspLoading = true
    const r = await api(`/api/sessions/${inspRun}/transcript?path=${encodeURIComponent(node.transcript)}`)
    inspLoading = false
    inspText = (r?.text && r.text.trim()) || r?.error || '(empty transcript)'
  }
  // Parse a run id like "20260620-234313" → readable "Jun 20, 23:43".
  function runWhen(id) {
    const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(id || '')
    if (!m) return ''
    const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6])
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  }

  onMount(() => {
    loadAll(); loadTasks()
    poll = setInterval(() => { loadAll(); if (tab === 'sessions') refreshSelected(); }, 4000)
  })
  onDestroy(() => { poll && clearInterval(poll) })

  const gb = mb => mb == null ? '-' : (mb / 1024).toFixed(1)
  const pct = (a, b) => b ? Math.min(100, Math.round(100 * a / b)) : 0
  function fmtTime(ts) {
    try {
      const d = new Date(ts < 2e10 ? ts * 1000 : ts)
      return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    } catch { return '' }
  }
  // epoch seconds from either an ISO string or a float/int timestamp
  const toEpoch = ts => {
    if (ts == null) return 0
    if (typeof ts === 'number') return ts < 2e10 ? ts : ts / 1000
    const p = Date.parse(ts); return isNaN(p) ? 0 : p / 1000
  }
  const nState = n => {
    const s = (n?.status || n?.state || '').toLowerCase()
    if (s.includes('run') || s.includes('act') || s.includes('writ')) return 'act'
    if (s.includes('wait') || s.includes('queue') || s.includes('gate')) return 'wait'
    if (s.includes('done') || s.includes('ok') || s.includes('accept') || s.includes('land')) return 'ok'
    return 'idle'
  }
  // outcome class for a finished/running workflow's status pill
  const runClass = status => {
    const s = (status || '').toLowerCase()
    if (s.includes('run') || s.includes('act')) return 'act'
    if (s.includes('land') || s.includes('accept') || s.includes('ok') || s.includes('done')) return 'ok'
    if (s.includes('fail') || s.includes('error') || s.includes('crash')) return 'err'
    if (s.includes('reject') || s.includes('denied')) return 'rej'
    return 'idle'   // no_changes, pending, etc.
  }
  // Section A · workflows - newest first + summary counts.
  let sortedRuns = $derived([...runs].sort((a, b) =>
    String(b.id || b.run_id || '').localeCompare(String(a.id || a.run_id || ''))))
  let richMap = $derived(Object.fromEntries((richRuns || []).map(r => [r.id, r])))
  const baseName = p => (p || '').split(/[\\/]/).pop()
  let runningCount = $derived(runs.filter(r => runClass(r.status) === 'act').length)
  let activeRunning = $derived(runningCount > 0)
  let teamWorkerCount = $derived((tree?.teams || []).reduce((s, t) => s + (t.workers?.length || 0), 0))

  let runs      = $derived(sessions?.runs || [])
  let cards     = $derived(gpu?.cards || [])
  let sbudget   = $derived(sessions?.budget || null)

  let gpuFreeGb = $derived(cards.reduce((s, c) => s + (c.free_mb || 0), 0) / 1024)
  let gpuStateSummary = $derived.by(() => {
    const n = {}
    for (const c of cards) n[c.state] = (n[c.state] || 0) + 1
    const parts = []
    if (n.ai) parts.push(`${n.ai} AI`)
    if (n.gaming) parts.push(`${n.gaming} gaming`)
    if (n.idle) parts.push(`${n.idle} idle`)
    return `${cards.length} card${cards.length === 1 ? '' : 's'}${parts.length ? ' · ' + parts.join(' · ') : ''}`
  })
  let sbudgetSummary = $derived.by(() => {
    if (!sbudget) return 'no budget data'
    const rows = Object.entries(sbudget).filter(([, b]) => b && typeof b === 'object' && b.cap != null)
    return rows.length ? rows.map(([t, b]) => `${t} ${b.used ?? 0}/${b.cap}`).join(' · ') : 'no caps'
  })
</script>

{#snippet anode(n, depth, runId)}
  {#if n}
    <div class="tnode clickable" style="padding-left:{depth * 18}px"
      onclick={() => openInspector(runId, n)} role="button" tabindex="0"
      title="click to inspect this step">
      <span class="s {nState(n)}"></span>
      <span class="who">{n.name || n.id}</span>
      {#if n.tier}<span class="mdl">· {n.tier}</span>{/if}
      {#if n.doing}<span class="doing">{n.doing}</span>{/if}
      <span class="peek">›</span>
    </div>
  {/if}
{/snippet}

{#snippet branchnode(n, depth)}
  {#if n}
    <div class="branch" class:worker={depth >= 2} style="margin-left:{depth * 20}px">
      {#if depth > 0}<span class="branch-elbow"></span>{/if}
      <div class="branch-card">
        <div class="branch-hd">
          <span class="s {nState(n)}"></span>
          <b class="branch-name">{n.name || n.id}</b>
          {#if n.tier}<span class="branch-tier">{n.tier}</span>{/if}
          <span class="wf-pill {runClass(n.doing || n.status)}">{n.doing || n.status || '-'}</span>
          {#if n.transcript}
            <button class="branch-view" onclick={() => selectInspNode(n)}
              class:on={inspNode?.id === n.id}>transcript ›</button>
          {/if}
        </div>
        <div class="branch-sum">
          {#if inspSummaries[n.id]}{inspSummaries[n.id]}
          {:else if inspSumLoading}<span class="sumwait">summarizing what it did…</span>
          {:else}<span class="muted">{n.doing || 'no summary'}</span>{/if}
        </div>
      </div>
    </div>
  {/if}
{/snippet}

<div class="orch">
  <header class="ohead">
    <div class="brand"><span class="dot"></span> Orchestrator
      <span class="tagline">control room · agents · GPUs</span></div>
    <div class="daemons">
      <span class="dm" class:up={sources.gpu_broker}><i></i>gpu broker</span>
      <span class="dm" class:up={sources.session_broker}><i></i>session broker</span>
    </div>
  </header>

  <nav class="tabbar">
    <button class="tabpill" class:on={tab === 'control'} onclick={() => tab = 'control'}>Control Room</button>
    <button class="tabpill" class:on={tab === 'sessions'} onclick={() => { tab = 'sessions'; loadTasks() }}>Sessions</button>
  </nav>

  {#if tab === 'control'}
  <!-- Overview -->
  <section class="panel usagebar">
    <div class="subhd">Overview</div>
    <div class="ustiles">
      <div class="utile">
        <div class="ut-h">SESSION BROKER</div>
        <div class="ut-big">{runs.length} <span class="ut-unit">runs</span></div>
        <div class="ut-sub">{sbudgetSummary}</div>
      </div>
      <div class="utile">
        <div class="ut-h">GPU / VRAM</div>
        <div class="ut-big">{gpuFreeGb.toFixed(1)} <span class="ut-unit">GB free</span></div>
        <div class="ut-sub">{gpuStateSummary}</div>
      </div>
    </div>
  </section>

  <div class="hero">
    <!-- A · Live agent tree -->
    <section class="panel">
      <div class="subhd">Workflows <span class="uhint">- {runs.length} on record{activeRunning ? ` · ${runningCount} running` : ''}</span></div>
      {#if !sessions?.up}
        <div class="down">⚠ session broker offline ({cfg.session_broker_url}), start it to see workflows</div>
      {:else}
        {#if tree}
          <!-- active / latest workflow - rich header + tree -->
          <div class="wa-top">
            <span class="s {nState(tree)}"></span>
            <span class="wa-tag">{activeRunning ? 'ACTIVE' : 'LATEST'}</span>
            <span class="wa-goal">{tree.summary?.goal || tree.run || activeRun}</span>
            <span class="wf-pill {runClass(tree.status)}">{tree.status || '-'}</span>
            <button class="inspectbtn" onclick={() => openInspector(activeRun)}>open ›</button>
          </div>
          <div class="wa-stats">
            <span>ran {runWhen(activeRun)}</span>
            <span>round {tree.round}/{tree.rounds_total}</span>
            {#if tree.summary?.spend != null}<span>~${(Number(tree.summary.spend) || 0).toFixed(2)}</span>{/if}
            <span>{(tree.teams || []).length} team{(tree.teams || []).length === 1 ? '' : 's'}</span>
            <span>{teamWorkerCount} worker{teamWorkerCount === 1 ? '' : 's'}</span>
            <span>{(tree.lateral || []).length} dept{(tree.lateral || []).length === 1 ? '' : 's'}</span>
          </div>
          <div class="tree wa-tree">
            {@render anode(tree.conductor, 0, activeRun)}
            {#each tree.teams || [] as team}
              {@render anode(team.lead, 0, activeRun)}
              {#each team.workers || [] as w}{@render anode(w, 1, activeRun)}{/each}
            {/each}
            {#each tree.lateral || [] as dep}
              {@render anode(dep, 0, activeRun)}
              {#each dep.members || dep.workers || dep.reviewers || [] as m}{@render anode(m, 1, activeRun)}{/each}
            {/each}
          </div>
        {/if}

        <div class="subhd wf-listhd">RECENT WORKFLOWS · click any to inspect</div>
        {#if !runs.length}<div class="muted">No workflows yet - launch one from the Sessions tab.</div>{/if}
        <div class="wf-list">
          {#each sortedRuns as r}
            {@const rid = r.id || r.run_id}
            {@const rr = richMap[rid] || {}}
            <div class="wf-row" class:sel={rid === activeRun}>
              <button class="wf-main" onclick={() => openInspector(rid)} title="inspect this workflow">
                <span class="wf-pill {runClass(r.status)}">{r.status || '?'}</span>
                <span class="wf-goal">{r.goal || r.slug || rid}</span>
                <span class="wf-meta">
                  {#if rr.target}<span class="wm file" title={rr.target}>{baseName(rr.target)}</span>{/if}
                  {#if rr.tier}<span class="wm tier">{rr.tier}</span>{/if}
                  {#if rr.spend != null}<span class="wm">~${(Number(rr.spend) || 0).toFixed(2)}</span>{/if}
                  {#if rr.recommendation}<span class="wm rec">{rr.recommendation}</span>{/if}
                  {#if rr.tests && (rr.tests.passed != null || rr.tests.failed != null)}<span class="wm">{rr.tests.passed ?? 0}✓/{rr.tests.failed ?? 0}✗</span>{/if}
                </span>
                <span class="wf-when">{runWhen(rid)}</span>
              </button>
              <button class="wf-arch" title="archive (move out of runs - reversible)" onclick={(e) => archiveRun(rid, r.goal, e)}>⌫</button>
            </div>
          {/each}
        </div>

        <div class="tbar">
          <span class="muted">{runningCount} running · {runs.length} on record</span>
          <button class="killsw" onclick={togglePause}>{sessions?.paused ? '▶ resume' : '⏸ pause'}</button>
        </div>
      {/if}
    </section>

    <!-- B · GPU / VRAM & leases -->
    <section class="panel">
      <div class="subhd">GPU / RAM &amp; VRAM</div>
      {#if !cards.length}<div class="muted">No GPU telemetry.</div>{/if}
      {#each cards as c}
        {@const free = c.free_mb}
        {@const st = c.state || 'idle'}
        <div class="gcard" class:gaming={st === 'gaming'}>
          <div class="gtop">
            <span class="gname">{c.name}</span>
            <span class="chip">{gb(c.total_mb)} GB</span>
            <span class="state {st}">{st === 'ai' ? 'AI' : st === 'gaming' ? 'GAMING' : 'idle'}</span>
          </div>
          {#if c.total_mb}
            <div class="vram"><span class="vlab">VRAM</span>
              <span class="vtrack"><i class="{st === 'gaming' ? 'seg-game' : 'seg-ai'}" style="width:{pct(c.used_mb || 0, c.total_mb)}%"></i></span>
              <span class="vnum">{gb(c.used_mb)} / {gb(c.total_mb)} GB</span></div>
          {/if}
          <div class="gmeta">
            {#if c.util != null}<span>util {c.util}%</span>{/if}
            {#if c.temp != null}<span>{c.temp}°C</span>{/if}
            {#if free != null}<span class:tight={free < 1500}>free {gb(free)} GB</span>{/if}
          </div>
          {#if st === 'gaming' && c.foreign_holder}
            <div class="holder game">▮ {c.foreign_holder} <span class="muted">non-AI · not the broker</span></div>
          {/if}
          {#if c.lease?.holders?.length}
            {#each c.lease.holders as h}
              <div class="holder"><span class="pulse"></span>{h.who || h}
                {#if h.held_for != null}<span class="held">held {Math.round(h.held_for)}s</span>{/if}</div>
            {/each}
          {/if}
          {#if c.lease?.queue?.length}
            <div class="queue">queue {#each c.lease.queue as q}<span class="qi">{q.who || q} {q.est_mb ? (q.est_mb/1024).toFixed(1)+'G' : ''}</span>{/each}</div>
          {/if}
          {#if free != null}
            <div class="preload">
              <div class="pl-hd">FITS IN {gb(free)} GB FREE</div>
              {#each PRELOAD as m}
                <div class="pl-row">
                  <span class="m">{m.name}</span><span class="req">{(m.mb/1024).toFixed(1)}G</span>
                  <span class="fit {m.mb <= free ? 'y' : 'n'}">{m.mb <= free ? 'fits' : 'too big'}</span>
                  <button class="loadbtn" class:live={m.mb <= free}
                    disabled={m.mb > free || loadingModel != null}
                    onclick={() => preloadModel(m, c)}
                    title={st === 'gaming' ? 'card is gaming - confirm will warn' : 'preload into free VRAM'}>
                    {loadingModel === m.name ? 'loading…' : 'load'}</button>
                </div>
              {/each}
              {#if loadErr}<div class="pl-err">{loadErr}</div>{/if}
            </div>
          {/if}
        </div>
      {/each}
      {#if gpu?.host_ram}
        {@const h = gpu.host_ram}
        <div class="gcard">
          <div class="gtop"><span class="gname">Host RAM</span><span class="chip">{gb(h.capacity_mb || h.budget_mb)} GB budget</span></div>
          <div class="vram"><span class="vlab">used</span>
            <span class="vtrack"><i class="seg-ai" style="width:{pct(h.used_mb || 0, h.budget_mb || 1)}%"></i></span>
            <span class="vnum">{gb(h.used_mb)} / {gb(h.budget_mb)} GB</span></div>
          <div class="gmeta"><span>reserved {gb(h.reserved_mb)} GB</span><span>free {gb(h.free_mb)} GB</span></div>
        </div>
      {:else if !gpu?.broker_up}
        <div class="down small">lease broker offline ({cfg.gpu_broker_url}), showing telemetry only</div>
      {/if}
    </section>
  </div>

  <div class="grid">
    <!-- D · Session runs + F · Session budget -->
    <section class="panel">
      <div class="subhd">Session runs</div>
      {#if !sessions?.up}<div class="down small">session broker offline</div>
      {:else if !runs.length}<div class="muted">No runs.</div>
      {:else}
        <div class="subhd">{runs.filter(r => runClass(r.status) === 'act').length} running &middot; click a workflow to inspect its agents + transcripts</div>
        {#each runs.slice(0, 8) as r}
          {@const rid = r.id || r.run_id}
          <button class="run runbtn" onclick={() => openInspector(rid)}>
            <span class="wf-pill {runClass(r.status)}">{r.status || '?'}</span>
            <span class="g">{r.goal || r.slug || rid}</span>
            <span class="rwhen">{runWhen(rid)}</span></button>
        {/each}
      {/if}
      {#if sbudget}
        <div class="subhd" style="margin-top:.9rem">Session budget</div>
        {#each Object.entries(sbudget) as [tier, b]}
          {#if b && typeof b === 'object' && b.cap != null}
            <div class="cap"><span class="cn">{tier}</span>
              <span class="ct"><i style="width:{pct(b.used || 0, b.cap)}%"></i></span>
              <span class="cv">{b.used ?? 0} / {b.cap}</span></div>
          {/if}
        {/each}
      {/if}
    </section>

  </div>
  {/if}

  {#if tab === 'sessions'}
  <div class="grid">
    <!-- A · New run -->
    <section class="panel">
      <div class="subhd">New run</div>
      {#if !sessions?.up}
        <div class="down">⚠ session broker offline ({cfg.session_broker_url})</div>
      {:else}
        {#if tasks.length}
          <div class="subhd">QUICK-LAUNCH TEMPLATE</div>
          {#each tasks as t}
            <div class="tpl">
              <div class="tpl-main"><span class="tpl-goal">{t.goal}</span>
                {#if t.repo}<span class="tpl-repo">{t.repo}</span>{/if}</div>
              <button class="glassbtn" disabled={launching} onclick={() => launchTask(t.file)}>launch</button>
            </div>
          {/each}
          <div class="subhd" style="margin-top:.7rem">OR CUSTOM</div>
        {:else}
          <div class="subhd">NEW RUN</div>
        {/if}
        <input class="tin" placeholder="goal - what should the run accomplish" bind:value={lgGoal} />
        <input class="tin" placeholder="repo_path (e.g. /path/to/repo)" bind:value={lgRepo} />
        <input class="tin" placeholder="target_file (optional)" bind:value={lgTarget} />
        <button class="glassbtn primary" disabled={launching || !lgGoal.trim()} onclick={() => launchTask(null)}>
          {launching ? 'launching…' : 'Launch run'}</button>
        {#if launchMsg}<div class="lmsg" class:err={launchMsg.startsWith('error') || launchMsg.includes('failed')}>{launchMsg}</div>{/if}
      {/if}
    </section>

    <!-- B · Runs -->
    <section class="panel">
      <div class="subhd">Runs</div>
      {#if !sessions?.up}<div class="down small">session broker offline</div>
      {:else if !runs.length}<div class="muted">No runs yet - launch one.</div>
      {:else}
        {#each runs as r}
          {@const rid = r.id || r.run_id}
          <button class="run runbtn" class:sel={rid === selRunId} onclick={() => selectRun(rid)}>
            <span class="st {nState(r)}"></span>
            <span class="g">{r.goal || r.slug || rid}</span>
            <span class="rmeta">{r.status || ''}</span></button>
        {/each}
      {/if}
    </section>

    <!-- C · Run + D · Gate -->
    <section class="panel">
      <div class="subhd">Run</div>
      {#if !selRunId}
        <div class="muted">Select a run to see its live tree.</div>
      {:else if !selTree}
        <div class="muted">No tree for {selRunId} (may be pending or finished).</div>
      {:else}
        <div class="treehd">
          <span class="s {nState(selTree)}"></span>
          <span class="goal">{selTree.summary?.goal || selTree.run || selRunId}</span>
          <span class="rd">round {selTree.round}/{selTree.rounds_total} · {selTree.status}</span>
        </div>
        <div class="tree">
          {@render anode(selTree.conductor, 0, selRunId)}
          {#each selTree.teams || [] as team}
            {@render anode(team.lead, 0, selRunId)}
            {#each team.workers || [] as w}{@render anode(w, 1, selRunId)}{/each}
          {/each}
          {#each selTree.lateral || [] as dep}
            {@render anode(dep, 0, selRunId)}
            {#each dep.members || dep.workers || dep.reviewers || [] as m}{@render anode(m, 1, selRunId)}{/each}
          {/each}
        </div>
      {/if}

      {#if selRunId && selGate?.pending}
        <div class="subhd" style="margin-top:.9rem">Gate - {selGate.pending}</div>
        <div class="gatebox">
          <div class="gate-pay">{JSON.stringify(selGate.payload || {}, null, 1).slice(0, 600)}</div>
          <div class="gate-act">
            <button class="glassbtn ok" onclick={() => decideGate(true)}>Approve</button>
            <button class="glassbtn no" onclick={() => decideGate(false)}>Reject</button>
          </div>
        </div>
      {:else if selRunId}
        <div class="subhd" style="margin-top:.9rem">Gate</div>
        <div class="muted">No gate awaiting a decision.</div>
      {/if}
    </section>
  </div>
  {/if}


  {#if inspRun}
    {@const d = inspDetail || {}}
    {@const rec = d.record || {}}
    {@const sm = inspTree?.summary || {}}
    <div class="inspback" onclick={closeInspector} role="presentation">
      <div class="inspwrap" onclick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <div class="insphead">
          <div class="insphd-l">
            <div class="inspgoal">{sm.goal || inspRun}</div>
            <div class="inspmeta">
              <span class="ipill {nState(inspTree || rec)}">{inspTree?.status || rec.status || '-'}</span>
              <span>ran {runWhen(inspRun) || inspRun}</span>
              {#if inspTree?.round}<span>round {inspTree.round}/{inspTree.rounds_total}</span>{/if}
              <span>spend ~${(Number(sm.spend) || 0).toFixed(2)}</span>
            </div>
          </div>
          <button class="inspx" onclick={closeInspector} title="close">×</button>
        </div>

        <div class="inspfacts">
          {#if sm.repo}<span class="fact"><b>repo</b> {sm.repo}</span>{/if}
          {#if sm.target}<span class="fact"><b>file</b> {sm.target}</span>{/if}
          {#if sm.branch}<span class="fact"><b>branch</b> {sm.branch}</span>{/if}
          {#if sm.commit}<span class="fact"><b>commit</b> {String(sm.commit).slice(0, 8)}</span>{/if}
          {#if sm.recommendation}<span class="fact"><b>review</b> {sm.recommendation}</span>{/if}
          {#if sm.tests && (sm.tests.passed != null || sm.tests.failed != null)}
            <span class="fact"><b>tests</b> {sm.tests.passed ?? 0}✓ / {sm.tests.failed ?? 0}✗</span>{/if}
        </div>

        <div class="inspbody">
          <div class="insptree">
            <div class="subhd">WORKFLOW &mdash; what each agent did
              {#if inspSumLoading}<span class="sumwait">· summarizing locally…</span>{/if}</div>
            {#if !inspTree}<div class="muted">Loading workflow…</div>
            {:else}
              <div class="branches">
                {@render branchnode(inspTree.conductor, 0)}
                {#each inspTree.teams || [] as team}
                  {@render branchnode(team.lead, 1)}
                  {#each team.workers || [] as w}{@render branchnode(w, 2)}{/each}
                {/each}
                {#each inspTree.lateral || [] as dep}
                  {@render branchnode(dep, 1)}
                  {#each dep.members || dep.workers || dep.reviewers || [] as m}{@render branchnode(m, 2)}{/each}
                {/each}
              </div>
            {/if}
          </div>
          <div class="inspnode">
            {#if !inspNode}
              <div class="muted">Click <b>transcript</b> on any agent to read its full proposal / output.</div>
            {:else}
              <div class="ndhd">
                <span class="s {nState(inspNode)}"></span>
                <b>{inspNode.name || inspNode.id}</b>
                {#if inspNode.tier}<span class="mdl">· {inspNode.tier}</span>{/if}
                <span class="nddoing">{inspNode.doing || ''}</span>
              </div>
              {#if inspLoading}<div class="muted">loading transcript…</div>
              {:else}<pre class="ndtext">{inspText}</pre>{/if}
            {/if}
          </div>
        </div>
      </div>
    </div>
  {/if}

</div>

<style>
  /* Fluid: fill the full viewport width with gutters + spacing + type that scale
     with the screen. No fixed max-width, so there's no dead space left/right. */
  .orch { width: 100%; max-width: 100%; margin: 0;
    padding: 1rem clamp(.9rem, 2.2vw, 2.6rem) 4rem;
    font-size: clamp(12.5px, .28vw + 11.7px, 14.5px); line-height: 1.45; }
  .ohead { display: flex; align-items: center; gap: 1rem; padding: 0 .2rem 1rem; flex-wrap: wrap; }
  .brand { font-weight: 700; font-size: 1.05rem; display: flex; align-items: center; gap: .5rem; }
  .brand .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 12px var(--accent); }
  .tagline { font-weight: 400; font-size: .72rem; color: var(--text-muted); font-family: monospace; }
  .daemons { margin-left: auto; display: flex; gap: .4rem; }
  .dm { display: flex; align-items: center; gap: .35rem; font-family: monospace; font-size: .68rem; color: var(--text-muted);
    border: 1px solid var(--border); border-radius: 8px; padding: .28rem .55rem;
    background: rgba(255,255,255,.04); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
  .dm i { width: 6px; height: 6px; border-radius: 50%; background: #ff6b8a; box-shadow: 0 0 6px #ff6b8a; }
  .dm.up i { background: #3ad29f; box-shadow: 0 0 6px #3ad29f; }

  /* tab bar */
  .tabbar { display: flex; gap: .5rem; margin: 0 .2rem 1rem; }
  .tabpill { font-family: monospace; font-size: .8rem; color: var(--text-muted); cursor: pointer;
    padding: .4rem 1rem; border-radius: 10px; border: 1px solid var(--border);
    background: rgba(255,255,255,.04); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    transition: background .15s, color .15s; }
  .tabpill:hover { background: rgba(255,255,255,.09); color: var(--text); }
  .tabpill.on { color: #04231e; background: var(--accent); border-color: var(--accent); font-weight: 700; box-shadow: 0 0 16px rgba(79,230,208,.35); }

  /* sessions tab */
  .tpl { display: flex; align-items: center; gap: .5rem; padding: .35rem 0; border-bottom: 1px solid var(--border); }
  .tpl:last-of-type { border: none; }
  .tpl-main { flex: 1; min-width: 0; }
  .tpl-goal { font-size: .8rem; display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tpl-repo { font-family: monospace; font-size: .62rem; color: var(--text-faint); }
  .tin { width: 100%; margin: .3rem 0; padding: .45rem .6rem; border-radius: 9px;
    background: rgba(0,0,0,.25); border: 1px solid var(--border); color: var(--text); font-size: .78rem; font-family: inherit; }
  .tin::placeholder { color: var(--text-faint); }
  .tin:focus { outline: none; border-color: var(--accent); }
  .glassbtn { font-family: monospace; font-size: .72rem; color: var(--text); cursor: pointer;
    padding: .35rem .8rem; border-radius: 9px; border: 1px solid var(--border);
    background: rgba(255,255,255,.055); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    transition: background .15s; }
  .glassbtn:hover:not(:disabled) { background: rgba(255,255,255,.12); }
  .glassbtn:disabled { opacity: .45; cursor: not-allowed; }
  .glassbtn.primary { width: 100%; margin-top: .4rem; color: #04231e; background: var(--accent); border-color: var(--accent); font-weight: 700; }
  .glassbtn.ok { color: #06251c; background: #3ad29f; border-color: #3ad29f; }
  .glassbtn.no { color: #2a0d13; background: #ff6b8a; border-color: #ff6b8a; }
  .lmsg { margin-top: .5rem; font-family: monospace; font-size: .72rem; color: #3ad29f; }
  .lmsg.err { color: #ff6b8a; }
  .runbtn { width: 100%; text-align: left; background: transparent; border: none;
    border-bottom: 1px solid var(--border); cursor: pointer; color: inherit; }
  .runbtn:hover { background: rgba(255,255,255,.04); }
  .runbtn.sel { background: rgba(79,230,208,.1); }
  .gatebox { background: rgba(0,0,0,.22); border: 1px solid var(--border); border-radius: 10px; padding: .6rem; }
  .gate-pay { font-family: monospace; font-size: .66rem; color: var(--text-muted); white-space: pre-wrap; max-height: 180px; overflow: auto; margin-bottom: .6rem; }
  .gate-act { display: flex; gap: .5rem; }

  /* clickable tree nodes + inspect affordances */
  .tnode.clickable { cursor: pointer; border-radius: 6px; padding-right: .3rem; }
  .tnode.clickable:hover { background: rgba(255,255,255,.05); }
  .tnode .peek { margin-left: .3rem; color: var(--text-faint); opacity: 0; font-weight: 700; }
  .tnode.clickable:hover .peek { opacity: 1; }
  .rwhen { font-family: monospace; font-size: .6rem; color: var(--text-faint); margin-left: auto; padding: 0 .5rem; white-space: nowrap; }

  /* workflow inspector modal */
  .inspback { position: fixed; inset: 0; z-index: 50; background: rgba(4,6,10,.62);
    backdrop-filter: blur(3px); display: flex; align-items: center; justify-content: center; padding: 3vh 3vw; }
  .inspwrap { width: min(1100px, 96vw); max-height: 92vh; display: flex; flex-direction: column;
    border-radius: 16px; border: 1px solid var(--border); overflow: hidden;
    background: linear-gradient(160deg, rgba(21,25,37,.96), rgba(13,16,23,.97));
    backdrop-filter: blur(30px) saturate(140%); box-shadow: 0 30px 80px rgba(0,0,0,.6); }
  .insphead { display: flex; align-items: flex-start; gap: 1rem; padding: 1rem 1.1rem .7rem; border-bottom: 1px solid var(--border); }
  .inspgoal { font-weight: 700; font-size: 1rem; }
  .inspmeta { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; font-family: monospace; font-size: .66rem; color: var(--text-muted); margin-top: .3rem; }
  .ipill { font-weight: 700; padding: .1rem .5rem; border-radius: 5px; background: rgba(255,255,255,.06); }
  .ipill.act { color: #37e0c8; } .ipill.ok { color: #3ad29f; } .ipill.wait { color: #ffb454; } .ipill.idle { color: var(--text-muted); }
  .inspx { margin-left: auto; background: transparent; border: 1px solid var(--border); color: var(--text);
    border-radius: 8px; width: 30px; height: 30px; font-size: 1.1rem; cursor: pointer; line-height: 1; flex: none; }
  .inspx:hover { background: rgba(255,255,255,.1); }
  .inspfacts { display: flex; flex-wrap: wrap; gap: .4rem .9rem; padding: .6rem 1.1rem; border-bottom: 1px solid var(--border);
    font-family: monospace; font-size: .66rem; color: var(--text-muted); }
  .fact b { color: var(--text-faint); font-weight: 600; margin-right: .3rem; }
  .inspbody { display: grid; grid-template-columns: minmax(380px, 1.25fr) 1fr; min-height: 0; flex: 1; overflow: hidden; }
  .insptree { padding: .8rem 1rem; border-right: 1px solid var(--border); overflow: auto; }
  .inspnode { padding: .8rem 1rem; overflow: auto; min-width: 0; }
  .sumwait { color: var(--text-faint); font-style: italic; font-family: inherit; }

  /* Branch tree - each agent its own card, summary of what it did, branching down */
  .branches { position: relative; }
  .branch { position: relative; margin-bottom: .5rem; }
  .branch-elbow { position: absolute; left: -12px; top: -6px; width: 12px; height: 24px;
    border-left: 1px solid rgba(79,230,208,.3); border-bottom: 1px solid rgba(79,230,208,.3);
    border-bottom-left-radius: 8px; }
  .branch-card { background: rgba(255,255,255,.03); border: 1px solid var(--border);
    border-radius: 10px; padding: .5rem .6rem; }
  .branch.worker .branch-card { background: rgba(255,255,255,.015); }
  .branch-hd { display: flex; align-items: center; gap: .45rem; flex-wrap: wrap; }
  .branch-name { font-size: .82rem; }
  .branch-tier { font-family: monospace; font-size: .62rem; color: #6ea8ff;
    background: rgba(110,168,255,.12); border-radius: 5px; padding: .05rem .35rem; }
  .branch-view { margin-left: auto; background: none; border: 1px solid var(--border);
    color: var(--text-muted); font-family: monospace; font-size: .6rem; cursor: pointer;
    border-radius: 6px; padding: .1rem .4rem; }
  .branch-view:hover, .branch-view.on { border-color: var(--accent); color: var(--accent); }
  .branch-sum { font-size: .74rem; line-height: 1.45; color: var(--text-muted); margin-top: .35rem; }
  .ndhd { display: flex; align-items: center; gap: .4rem; font-size: .85rem; margin-bottom: .5rem; }
  .ndhd .nddoing { margin-left: auto; font-family: monospace; font-size: .66rem; color: var(--text-faint); }
  .ndtext { font-family: monospace; font-size: .68rem; line-height: 1.5; white-space: pre-wrap; word-break: break-word;
    background: rgba(0,0,0,.28); border: 1px solid var(--border); border-radius: 8px; padding: .7rem; color: var(--text-muted); margin: 0; }
  @media (max-width: 720px) { .inspbody { grid-template-columns: 1fr; } .insptree { border-right: none; border-bottom: 1px solid var(--border); } }

  /* P0 · unified usage bar */
  .usagebar { margin-bottom: clamp(.6rem, 1vw, 1.2rem); }
  .uhint { color: var(--text-faint); font-weight: 400; text-transform: none; letter-spacing: 0; }
  .ustiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: .8rem; }
  .utile { padding: .1rem .2rem; border-left: 2px solid var(--border); padding-left: .7rem; }
  .ut-h { font-family: monospace; font-size: .6rem; letter-spacing: .05em; color: var(--text-faint);
    display: flex; align-items: center; gap: .4rem; margin-bottom: .2rem; }
  .ut-big { font-size: 1.35rem; font-weight: 700; font-family: monospace; letter-spacing: -.02em; }
  .ut-unit { font-size: .72rem; color: var(--text-muted); font-weight: 400; }
  .ut-sub { font-family: monospace; font-size: .64rem; color: var(--text-muted); margin-top: .15rem; }

  .hero { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr);
    gap: clamp(.6rem, 1vw, 1.2rem); margin-bottom: clamp(.6rem, 1vw, 1.2rem); }
  /* auto-fit: columns grow to fill the row and reflow by available width */
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: clamp(.6rem, 1vw, 1.2rem); align-items: start; }
  .panel { position: relative; min-width: 0; padding: .95rem 1.05rem; border-radius: 18px;
    background: linear-gradient(155deg, rgba(255,255,255,.04), rgba(255,255,255,.008));
    border: 1px solid var(--border);
    backdrop-filter: blur(24px) saturate(150%); -webkit-backdrop-filter: blur(24px) saturate(150%);
    box-shadow: 0 12px 38px rgba(0,0,0,.42), inset 0 0 0 .5px rgba(255,255,255,.03); }
  .muted { color: var(--text-muted); font-size: .8rem; padding: .3rem 0; }
  .down { color: #ffb454; font-size: .8rem; padding: .4rem 0; }
  .down.small { font-size: .72rem; }
  .subhd { font-family: monospace; font-size: .62rem; color: var(--text-faint); letter-spacing: .05em; margin: .3rem 0; }

  /* recent workflows list */
  .wf-list { display: flex; flex-direction: column; gap: .25rem; }
  .wf-row { display: flex; align-items: stretch; width: 100%;
    background: rgba(255,255,255,.03); border: 1px solid var(--border); border-radius: 8px;
    overflow: hidden; transition: background .12s, border-color .12s; }
  .wf-row:hover { background: rgba(255,255,255,.07); border-color: rgba(79,230,208,.3); }
  .wf-main { flex: 1; min-width: 0; display: flex; align-items: center; gap: .5rem; text-align: left;
    background: transparent; border: none; color: inherit; font: inherit; cursor: pointer; padding: .35rem .5rem; }
  .wf-main .wf-goal { flex: 0 1 auto; max-width: 46%; }
  .wf-meta { flex: 1; display: flex; align-items: center; gap: .3rem; flex-wrap: wrap; min-width: 0; }
  .wm { font-family: monospace; font-size: .56rem; color: var(--text-muted); background: rgba(255,255,255,.045);
    border: 1px solid var(--border); border-radius: 4px; padding: .05rem .3rem; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; max-width: 160px; }
  .wm.file { color: #cbd3e2; } .wm.tier { color: #6ea8ff; } .wm.rec { color: #7ee0a0; }
  .wf-arch { flex: none; background: transparent; border: none; border-left: 1px solid var(--border);
    color: var(--text-faint); cursor: pointer; padding: 0 .55rem; font-size: .9rem; line-height: 1; }
  .wf-arch:hover { background: rgba(255,138,178,.14); color: #ff8ab2; }
  .wf-pill { flex: none; font-family: monospace; font-size: .58rem; text-transform: uppercase; letter-spacing: .03em;
    padding: .1rem .4rem; border-radius: 5px; border: 1px solid transparent; }
  .wf-pill.ok  { color: #7ee0a0; background: rgba(126,224,160,.12); border-color: rgba(126,224,160,.3); }
  .wf-pill.err { color: #ff8ab2; background: rgba(255,138,178,.12); border-color: rgba(255,138,178,.3); }
  .wf-pill.rej { color: #ffb454; background: rgba(255,180,84,.12); border-color: rgba(255,180,84,.3); }
  .wf-pill.act { color: #4fe6d0; background: rgba(79,230,208,.14); border-color: rgba(79,230,208,.35); }
  .wf-pill.idle{ color: var(--text-faint); background: rgba(255,255,255,.05); border-color: var(--border); }
  .wf-goal { flex: 1; font-size: .76rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .wf-when { flex: none; font-family: monospace; font-size: .62rem; color: var(--text-faint); }
  /* Section A · rich active/latest workflow header + bounded lists */
  .wa-top { display: flex; align-items: center; gap: .5rem; margin-bottom: .4rem; }
  .wa-tag { font-family: monospace; font-size: .56rem; font-weight: 700; letter-spacing: .06em; flex: none;
    color: #04231e; background: var(--accent); border-radius: 4px; padding: .1rem .4rem; }
  .wa-goal { flex: 1; font-weight: 600; font-size: .84rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .inspectbtn { flex: none; font-family: monospace; font-size: .62rem; color: var(--accent); cursor: pointer;
    background: transparent; border: 1px solid rgba(79,230,208,.35); border-radius: 6px; padding: .15rem .5rem; }
  .inspectbtn:hover { background: rgba(79,230,208,.12); }
  .wa-stats { display: flex; flex-wrap: wrap; gap: .3rem .8rem; font-family: monospace; font-size: .62rem;
    color: var(--text-muted); margin-bottom: .5rem; }
  .wa-tree { max-height: 230px; overflow: auto; border-left: 2px solid var(--border); padding-left: .3rem; }
  .wf-listhd { margin-top: .7rem; }
  .wf-list { max-height: 300px; overflow: auto; }
  .wf-row.sel { border-color: rgba(79,230,208,.4); background: rgba(79,230,208,.08); }

  .treehd { display: flex; align-items: center; gap: .5rem; margin-bottom: .5rem; }
  .treehd .goal { font-weight: 600; font-size: .82rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .treehd .rd { margin-left: auto; font-family: monospace; font-size: .64rem; color: #ffb454; flex: none; }
  .tree { font-family: monospace; font-size: .8rem; line-height: 1.7; }
  .tnode { display: flex; align-items: center; gap: .5rem; color: var(--text-muted); }
  .tnode .who { color: var(--text); }
  .tnode .mdl { color: #6ea8ff; font-size: .74rem; }
  .tnode .doing { margin-left: auto; color: var(--text-faint); font-size: .68rem; }
  .s { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--text-faint); }
  .s.act { background: #37e0c8; box-shadow: 0 0 6px #37e0c8; animation: bp 1.5s infinite; }
  .s.wait { background: #ffb454; } .s.ok { background: #3ad29f; } .s.idle { background: #5a6274; }
  @keyframes bp { 0%,100%{opacity:1} 50%{opacity:.3} }
  .tbar { display: flex; align-items: center; margin-top: .7rem; padding-top: .6rem; border-top: 1px solid var(--border); }
  .killsw { margin-left: auto; font-family: monospace; font-size: .72rem;
    background: rgba(255,255,255,.07); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    border: 1px solid var(--border); color: var(--text); border-radius: 8px; padding: .28rem .7rem; cursor: pointer;
    transition: background .15s; }
  .killsw:hover { background: rgba(255,255,255,.13); }

  .gcard { border-radius: 13px; padding: .62rem .72rem; margin-bottom: .55rem;
    background: linear-gradient(155deg, rgba(255,255,255,.038), rgba(255,255,255,.006));
    border: 1px solid rgba(255,255,255,.07);
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); }
  .gcard.gaming { border-color: rgba(185,140,255,.4); box-shadow: 0 0 22px rgba(185,140,255,.07); }
  .gtop { display: flex; align-items: center; gap: .5rem; margin-bottom: .4rem; }
  .gname { font-weight: 600; font-size: .82rem; }
  .chip { font-family: monospace; font-size: .62rem; padding: .12rem .45rem; border-radius: 6px; border: 1px solid var(--border); color: var(--text-muted); background: rgba(255,255,255,.045); }
  .state { margin-left: auto; font-family: monospace; font-size: .6rem; font-weight: 700; padding: .12rem .5rem; border-radius: 5px; }
  .state.ai { background: rgba(55,224,200,.15); color: #37e0c8; }
  .state.gaming { background: rgba(185,140,255,.15); color: #b98cff; }
  .state.idle { background: rgba(90,98,116,.15); color: var(--text-muted); }
  .vram { display: flex; align-items: center; gap: .5rem; margin: .25rem 0; }
  .vlab { flex: 0 0 34px; font-family: monospace; font-size: .62rem; color: var(--text-faint); }
  .vtrack { flex: 1; height: 9px; background: rgba(0,0,0,.28); border-radius: 5px; overflow: hidden; }
  .vtrack > i { display: block; height: 100%; }
  .seg-ai { background: linear-gradient(90deg,#6ea8ff,#37e0c8); }
  .seg-game { background: linear-gradient(90deg,#8a5cff,#b98cff); }
  .vnum { flex: 0 0 96px; text-align: right; font-family: monospace; font-size: .68rem; }
  .gmeta { display: flex; gap: .8rem; font-family: monospace; font-size: .66rem; color: var(--text-muted); margin-top: .3rem; }
  .gmeta .tight { color: #ff6b8a; }
  .holder { display: flex; align-items: center; gap: .4rem; font-family: monospace; font-size: .72rem; padding: .15rem 0; color: var(--text); }
  .holder.game { color: #b98cff; }
  .holder .held { margin-left: auto; color: var(--text-muted); }
  .pulse { width: 7px; height: 7px; border-radius: 50%; background: #37e0c8; box-shadow: 0 0 6px #37e0c8; animation: bp 1.5s infinite; }
  .queue { margin-top: .3rem; font-family: monospace; font-size: .64rem; color: var(--text-faint); display: flex; gap: .3rem; flex-wrap: wrap; }
  .qi { padding: .1rem .4rem; border: 1px dashed var(--border); border-radius: 5px; color: #ffb454; }
  .preload { margin-top: .45rem; padding-top: .45rem; border-top: 1px dashed var(--border); }
  .pl-hd { font-family: monospace; font-size: .58rem; color: var(--text-faint); margin-bottom: .3rem; }
  .pl-row { display: flex; align-items: center; gap: .45rem; font-family: monospace; font-size: .68rem; padding: .1rem 0; }
  .pl-row .m { color: var(--text); } .pl-row .req { color: var(--text-faint); }
  .fit { margin-left: auto; font-size: .6rem; } .fit.y { color: #3ad29f; } .fit.n { color: var(--text-faint); }
  .loadbtn { font-family: monospace; font-size: .58rem; border: 1px solid var(--border); color: var(--text-faint);
    background: transparent; border-radius: 5px; padding: .1rem .45rem; cursor: not-allowed; }
  .loadbtn.live:not(:disabled) { color: #37e0c8; border-color: rgba(55,224,200,.4); cursor: pointer;
    background: rgba(55,224,200,.08); }
  .loadbtn.live:not(:disabled):hover { background: rgba(55,224,200,.16); }
  .loadbtn:disabled { cursor: not-allowed; opacity: .7; }
  .pl-err { margin-top: .3rem; font-family: monospace; font-size: .62rem; color: #ff6b8a; }

  .run { display: flex; align-items: center; gap: .5rem; padding: .35rem 0; border-bottom: 1px solid var(--border); }
  .run:last-child { border: none; }
  .run .g { flex: 1; font-size: .78rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .run .rmeta { font-family: monospace; font-size: .64rem; color: var(--text-faint); }
  .cap { display: flex; align-items: center; gap: .5rem; padding: .2rem 0; }
  .cap .cn { flex: 0 0 70px; font-family: monospace; font-size: .68rem; color: var(--text-muted); }
  .cap .ct { flex: 1; height: 6px; background: rgba(0,0,0,.28); border-radius: 4px; overflow: hidden; }
  .cap .ct > i { display: block; height: 100%; background: linear-gradient(90deg,#2b8a6f,#3ad29f); }
  .cap .cv { flex: 0 0 62px; text-align: right; font-family: monospace; font-size: .66rem; }

  .big { font-size: 1.5rem; font-weight: 700; font-family: monospace; letter-spacing: -.02em; margin-bottom: .3rem; }
  .big small { font-size: .68rem; color: var(--text-muted); font-weight: 400; margin-left: .3rem; }
  .row { display: flex; align-items: center; gap: .5rem; padding: .2rem 0; }
  .row .name { flex: 0 0 118px; color: var(--text-muted); font-size: .7rem; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bar { flex: 1; height: 6px; background: rgba(0,0,0,.28); border-radius: 4px; overflow: hidden; }
  .bar > i { display: block; height: 100%; background: linear-gradient(90deg,#6ea8ff,#37e0c8); }
  .bar > i.uc { background: linear-gradient(90deg,#b98cff,#6ea8ff); }
  .val { flex: 0 0 50px; text-align: right; font-family: monospace; font-size: .7rem; }
  .fl { display: flex; gap: .5rem; padding: .12rem 0; color: var(--text-muted); }
  .fl .ft { color: var(--text-faint); } .fl .fu { color: #37e0c8; }
  .fl .fm { color: var(--text-faint); } .fl .fc { margin-left: auto; color: var(--text); }

  @media (max-width: 900px) { .hero { grid-template-columns: 1fr; } }
</style>
