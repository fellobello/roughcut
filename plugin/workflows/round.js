export const meta = {
  name: 'round',
  description: 'One roughcut round: plan, build every unblocked task in parallel, merge, test, and collect questions',
  whenToUse: 'Started by /roughcut:build and /roughcut:answer with args {project, pluginRoot, round}',
  phases: [
    { title: 'Plan', detail: 'turn the idea or the latest answers into tasks' },
    { title: 'Build', detail: 'one agent per unblocked task, merged wave by wave' },
    { title: 'Verify', detail: 'run every test on the merged code' },
    { title: 'Questions', detail: 'collect questions and decisions for the user' },
  ],
}

// One round of the loop. The user answers questions between rounds, never during one,
// so nothing here waits on them. All state lives in <project>/.hub and only hub.py writes it;
// this script decides what runs when, and the agents do the work.

const MAX_WAVES = 20 // per build pass; a longer dependency chain finishes next round
const MAX_FIX_PASSES = 2 // build passes after the first that only fix failing tests

if (!args || !args.project || !args.pluginRoot) {
  throw new Error('round needs args {project, pluginRoot, round}')
}
const project = args.project
const round = args.round || 0
// Tasks per wave. Small waves keep a round inside plan usage limits for longer,
// and when a limit does hit, fewer agents fail at once.
const maxParallel = args.maxParallel || 4
const HUB = `python3 "${args.pluginRoot}/scripts/hub.py" --project "${project}"`

const TASK = {
  type: 'object',
  required: ['id', 'kind', 'title', 'worktree', 'branch'],
  properties: {
    id: { type: 'string' },
    kind: { type: 'string', enum: ['build', 'test'] },
    title: { type: 'string' },
    worktree: { type: 'string' },
    branch: { type: 'string' },
  },
}
const WAVE_SCHEMA = { type: 'object', required: ['tasks'], properties: { tasks: { type: 'array', items: TASK } } }
const PLAN_SCHEMA = {
  type: 'object',
  required: ['todo', 'summary'],
  properties: { todo: { type: 'integer' }, summary: { type: 'string' } },
}
const RESULT_SCHEMA = {
  type: 'object',
  required: ['task', 'status', 'summary'],
  properties: {
    task: { type: 'string' },
    status: { type: 'string', enum: ['done', 'blocked', 'failed'] },
    summary: { type: 'string' },
  },
}
const MERGE_SCHEMA = {
  type: 'object',
  required: ['merged', 'sent_back', 'tests_passed'],
  properties: {
    merged: { type: 'array', items: { type: 'string' } },
    sent_back: { type: 'array', items: { type: 'string' } },
    tests_passed: { type: 'boolean' },
  },
}
const VERIFY_SCHEMA = {
  type: 'object',
  required: ['passed', 'reopened', 'summary'],
  properties: {
    passed: { type: 'boolean' },
    reopened: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
}
const REPORT_SCHEMA = {
  type: 'object',
  required: ['done', 'blocked', 'blocking_questions', 'guesses', 'next_round', 'report'],
  properties: {
    done: { type: 'array', items: { type: 'string' } },
    blocked: { type: 'array', items: { type: 'string' } },
    blocking_questions: { type: 'integer' },
    guesses: { type: 'integer' },
    next_round: { type: 'string' },
    report: { type: 'string' },
  },
}

function context(...lines) {
  return [`Project folder: ${project}`, `Round: ${round}`, ...lines].join('\n')
}

// Run waves until nothing is ready: each wave starts up to maxParallel unblocked tasks,
// then merges them all before the next wave, so later tasks build on merged code.
// Returns `stopped` with a reason when a step dies (a null result), usually at a usage
// limit: the round ends there, and its next run picks up the unfinished tasks.
async function buildPass(firstWave) {
  let wave = firstWave
  const results = []
  for (; wave < firstWave + MAX_WAVES; wave++) {
    const started = await agent(
      `Run this command and return the tasks it prints, unchanged:\n${HUB} start-wave --limit ${maxParallel}`,
      { label: `schedule:wave-${wave}`, phase: 'Build', effort: 'low', schema: WAVE_SCHEMA },
    )
    if (!started) return { nextWave: wave, results, stopped: `the scheduler for wave ${wave} stopped` }
    if (started.tasks.length === 0) break
    const ids = started.tasks.map(t => t.id)
    log(`Wave ${wave}: ${ids.join(', ')}`)
    const finished = await parallel(started.tasks.map(task => () => agent(
      context(
        `Your task: ${task.id} (${task.kind}): ${task.title}`,
        `Your worktree: ${task.worktree} (branch ${task.branch})`,
        task.kind === 'test' ? 'Job: write tests.' : '',
      ),
      {
        label: `${task.kind}:${task.id}`,
        phase: 'Build',
        agentType: task.kind === 'test' ? 'roughcut:tester' : 'roughcut:builder',
        schema: RESULT_SCHEMA,
      },
    )))
    const ok = finished.filter(Boolean)
    results.push(...ok)
    if (ok.length === 0) {
      return { nextWave: wave + 1, results, stopped: `every agent in wave ${wave} stopped (${ids.join(', ')})` }
    }
    const merged = await agent(context(`Wave ${wave} tasks: ${ids.join(', ')}`), {
      label: `merge:wave-${wave}`,
      phase: 'Build',
      agentType: 'roughcut:synthesizer',
      schema: MERGE_SCHEMA,
    })
    if (!merged) return { nextWave: wave + 1, results, stopped: `the merge of wave ${wave} stopped` }
  }
  return { nextWave: wave, results, stopped: null }
}

phase('Plan')
const plan = await agent(context(), {
  label: `plan:round-${round}`,
  phase: 'Plan',
  agentType: 'roughcut:planner',
  schema: PLAN_SCHEMA,
})
if (!plan) throw new Error('the planner stopped before finishing, so nothing was built')

const built = []
const checks = []
let stopped = null
if (plan.todo > 0) {
  phase('Build')
  let pass = await buildPass(1)
  built.push(...pass.results)
  stopped = pass.stopped
  if (!stopped) phase('Verify')
  for (let fix = 0; !stopped && fix <= MAX_FIX_PASSES; fix++) {
    const check = await agent(context('Job: verify the merged code.'), {
      label: `verify:${fix + 1}`,
      phase: 'Verify',
      agentType: 'roughcut:tester',
      schema: VERIFY_SCHEMA,
    })
    if (!check) {
      stopped = 'the verifier stopped'
      break
    }
    checks.push(check)
    if (check.passed || check.reopened.length === 0 || fix === MAX_FIX_PASSES) break
    log(`Fixing: ${check.reopened.join(', ')}`)
    pass = await buildPass(pass.nextWave)
    built.push(...pass.results)
    stopped = pass.stopped
  }
}
if (stopped) {
  // Skip the questions step: it would most likely stop the same way.
  log(`Round stopped: ${stopped}. Finished work is kept; run the round again to continue.`)
  return { round, plan, built, checks, report: null, stopped }
}

phase('Questions')
const report = await agent(context(), {
  label: 'questions',
  phase: 'Questions',
  agentType: 'roughcut:questioner',
  schema: REPORT_SCHEMA,
})

if (!report) return { round, plan, built, checks, report: null, stopped: 'the questions step stopped' }
return { round, plan, built, checks, report, stopped: null }
