export const meta = {
  name: 'round',
  description: 'One idea-builder round: plan, build every unblocked task in parallel, merge, test, and collect questions',
  whenToUse: 'Started by /idea-builder:build and /idea-builder:answer with args {project, pluginRoot, round}',
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

const MAX_WAVES = 10 // per build pass; a longer dependency chain finishes next round
const MAX_FIX_PASSES = 2 // build passes after the first that only fix failing tests

if (!args || !args.project || !args.pluginRoot) {
  throw new Error('round needs args {project, pluginRoot, round}')
}
const project = args.project
const round = args.round || 0
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

// Run waves until nothing is ready: each wave starts every unblocked task at once,
// then merges them all before the next wave, so later tasks build on merged code.
async function buildPass(firstWave) {
  let wave = firstWave
  const results = []
  for (; wave < firstWave + MAX_WAVES; wave++) {
    const started = await agent(
      `Run this command and return the tasks it prints, unchanged:\n${HUB} start-wave`,
      { label: `schedule:wave-${wave}`, phase: 'Build', effort: 'low', schema: WAVE_SCHEMA },
    )
    if (!started || started.tasks.length === 0) break
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
        agentType: task.kind === 'test' ? 'idea-builder:tester' : 'idea-builder:builder',
        schema: RESULT_SCHEMA,
      },
    )))
    results.push(...finished.filter(Boolean))
    await agent(context(`Wave ${wave} tasks: ${ids.join(', ')}`), {
      label: `merge:wave-${wave}`,
      phase: 'Build',
      agentType: 'idea-builder:synthesizer',
      schema: MERGE_SCHEMA,
    })
  }
  return { nextWave: wave, results }
}

phase('Plan')
const plan = await agent(context(), {
  label: `plan:round-${round}`,
  phase: 'Plan',
  agentType: 'idea-builder:planner',
  schema: PLAN_SCHEMA,
})
if (!plan) throw new Error('the planner stopped before finishing, so nothing was built')

const built = []
const checks = []
if (plan.todo > 0) {
  phase('Build')
  let pass = await buildPass(1)
  built.push(...pass.results)
  phase('Verify')
  for (let fix = 0; fix <= MAX_FIX_PASSES; fix++) {
    const check = await agent(context('Job: verify the merged code.'), {
      label: `verify:${fix + 1}`,
      phase: 'Verify',
      agentType: 'idea-builder:tester',
      schema: VERIFY_SCHEMA,
    })
    if (check) checks.push(check)
    if (!check || check.passed || check.reopened.length === 0 || fix === MAX_FIX_PASSES) break
    log(`Fixing: ${check.reopened.join(', ')}`)
    pass = await buildPass(pass.nextWave)
    built.push(...pass.results)
  }
}

phase('Questions')
const report = await agent(context(), {
  label: 'questions',
  phase: 'Questions',
  agentType: 'idea-builder:questioner',
  schema: REPORT_SCHEMA,
})

return { round, plan, built, checks, report }
