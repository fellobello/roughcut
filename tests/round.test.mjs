// Tests for workflows/round.js: runs the real script with fake agents and checks
// which agents it starts and when it stops. Run with: node --test tests/round.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('../workflows/round.js', import.meta.url), 'utf8')
  .replace('export const meta', 'const meta')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const script = new AsyncFunction('args', 'agent', 'parallel', 'phase', 'log', source)

const ARGS = { project: '/p', pluginRoot: '/r', round: 0 }

// Runs one round. `respond(prompt, opts)` plays every agent; a null result is an agent that died.
async function runRound(respond, args = ARGS) {
  const calls = []
  const agent = async (prompt, opts) => {
    calls.push({ prompt, opts })
    return respond(prompt, opts)
  }
  const parallel = thunks => Promise.all(thunks.map(thunk => thunk().catch(() => null)))
  const result = await script(args, agent, parallel, () => {}, () => {})
  return { result, calls, labels: calls.map(c => c.opts.label) }
}

// A project whose scheduler hands out `waves` of task ids in order. `overrides` maps an
// agent label to the result that agent returns instead of the normal one.
function project(waves, overrides = {}) {
  let next = 0
  return (prompt, opts) => {
    const label = opts.label
    if (label in overrides) return overrides[label]
    if (label.startsWith('plan:')) return { todo: waves.flat().length, summary: 'plan' }
    if (label.startsWith('schedule:')) {
      const ids = waves[next++] || []
      return {
        tasks: ids.map(id => ({
          id,
          kind: id.endsWith('-tests') ? 'test' : 'build',
          title: id,
          worktree: `/p/.worktrees/${id}`,
          branch: `build/${id}-1`,
        })),
      }
    }
    if (label.startsWith('build:') || label.startsWith('test:')) {
      return { task: label.split(':')[1], status: 'done', summary: 'ok' }
    }
    if (label.startsWith('merge:')) return { merged: [], sent_back: [], tests_passed: true }
    if (label.startsWith('verify:')) return { passed: true, reopened: [], summary: 'ok' }
    if (label === 'questions') {
      return { done: [], blocked: [], blocking_questions: 0, guesses: 0, next_round: '', report: 'r.md' }
    }
    throw new Error(`unexpected agent ${label}`)
  }
}

test('a full round plans, builds wave by wave, verifies, then asks', async () => {
  const { result, labels, calls } = await runRound(project([['a', 'a-tests'], ['b']]))
  assert.deepEqual(labels, [
    'plan:round-0',
    'schedule:wave-1', 'build:a', 'test:a-tests', 'merge:wave-1',
    'schedule:wave-2', 'build:b', 'merge:wave-2',
    'schedule:wave-3',
    'verify:1',
    'questions',
  ])
  assert.equal(result.stopped, null)
  assert.equal(result.built.length, 3)
  assert.equal(calls.find(c => c.opts.label === 'test:a-tests').opts.agentType, 'roughcut:tester')
  assert.equal(calls.find(c => c.opts.label === 'build:a').opts.agentType, 'roughcut:builder')
})

test('waves are capped at 4 tasks unless args say otherwise', async () => {
  const first = await runRound(project([['a']]))
  assert.match(first.calls[1].prompt, /start-wave --limit 4$/)
  const wide = await runRound(project([['a']]), { ...ARGS, maxParallel: 8 })
  assert.match(wide.calls[1].prompt, /start-wave --limit 8$/)
})

test('a wave where every agent dies stops the round before merging or asking', async () => {
  const { result, labels } = await runRound(project([['a', 'b'], ['c']], { 'build:a': null, 'build:b': null }))
  assert.deepEqual(labels, ['plan:round-0', 'schedule:wave-1', 'build:a', 'build:b'])
  assert.match(result.stopped, /every agent in wave 1 stopped \(a, b\)/)
  assert.equal(result.report, null)
})

test('a wave where only some agents die still merges and carries on', async () => {
  const { result, labels } = await runRound(project([['a', 'b']], { 'build:a': null }))
  assert.ok(labels.includes('merge:wave-1'))
  assert.ok(labels.includes('questions'))
  assert.equal(result.stopped, null)
})

test('a dead merge, scheduler or verifier stops the round', async () => {
  for (const [label, reason] of [
    ['merge:wave-1', /the merge of wave 1 stopped/],
    ['schedule:wave-2', /the scheduler for wave 2 stopped/],
    ['verify:1', /the verifier stopped/],
  ]) {
    const { result, labels } = await runRound(project([['a'], ['b']], { [label]: null }))
    assert.match(result.stopped, reason, label)
    assert.equal(labels.at(-1), label, label)
  }
})

test('failing tests get a fix pass, then a second check', async () => {
  const { result, labels } = await runRound(project([['a'], [], ['a']], {
    'verify:1': { passed: false, reopened: ['a'], summary: 'a fails' },
  }))
  assert.deepEqual(labels.slice(labels.indexOf('verify:1')), [
    'verify:1', 'schedule:wave-2', 'build:a', 'merge:wave-2', 'schedule:wave-3', 'verify:2', 'questions',
  ])
  assert.equal(result.checks.length, 2)
})

test('fix passes stop after two tries', async () => {
  const failing = { passed: false, reopened: ['a'], summary: 'a fails' }
  const { labels } = await runRound(project([['a'], [], ['a'], [], ['a'], []], {
    'verify:1': failing, 'verify:2': failing, 'verify:3': failing,
  }))
  assert.equal(labels.filter(l => l.startsWith('verify:')).length, 3)
  assert.equal(labels.at(-1), 'questions')
})

test('a plan with nothing to do goes straight to questions', async () => {
  const { labels } = await runRound(project([], { 'plan:round-0': { todo: 0, summary: 'nothing' } }))
  assert.deepEqual(labels, ['plan:round-0', 'questions'])
})

test('a dead planner fails the round, and a dead questioner is reported', async () => {
  await assert.rejects(runRound(project([['a']], { 'plan:round-0': null })), /planner stopped/)
  const { result } = await runRound(project([['a']], { questions: null }))
  assert.equal(result.stopped, 'the questions step stopped')
})

test('missing args fail fast', async () => {
  await assert.rejects(runRound(project([]), { round: 0 }), /needs args/)
})
