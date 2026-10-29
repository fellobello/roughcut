# Test run: SplitTip (branch test-run-splittip)

Started 2026-10-28 13:22. Studio on :8771, models qwen2.5-coder:7b + qwen3:8b. Project: ~/roughcut-projects/u-build-tip.

## Log

### P1 — 13:22, brainstorm (no round): brainstormer argues against what the user will want, and re-asks answered questions
- Saw: first brainstormer reply proposed *removing* percentage split and *avoiding* dark mode, and recommended "Only equal split" / "No dark mode". After I answered both ("Both options", "Add dark mode") and then restated them plainly in a Talk message, the second reply asked the same three questions again (split mode, dark mode, plus rounding which I had just stated).
- Evidence: `.hub/brainstorm.json` entries 2 and 5 (last); `.hub/questions/hub.1..3.json`.
- Guess: prompt — the brainstormer isn't given the already-answered Q/A as settled facts (or isn't told not to re-ask), and its "simplify" bias picks the cut option as default. Code could also filter questions whose text matches an answered one. Model+prompt+code.
- Pointless questions so far: 3 (all re-asks).

### P2 — 13:27, brainstorm: brief keeps the receipt I said to skip, drops details
- Saw: `brief.json` nice_to_have lists "Printable receipt option" though both the idea answers and my Talk message said skip it. Default tip 15%, whole-dollar rounding, "no history of past splits" and "English only" aren't in the brief at all.
- Evidence: `.hub/brief.json` (nice_to_have[2]); `.hub/brainstorm.json` my message at index 5.
- Guess: model (qwen3:8b) summarising lossy; prompt doesn't make the brief schema carry "rules/defaults". Code could add a `rules` / `defaults` field. Prompt+model.

### UI notes (brainstorm)
- Change answer works: CHANGE → "Add dark mode" updated `questions/hub.4.json` (answer changed, choice kept as old) and appended "I changed my answer…" to brainstorm.json. OK.
- A hover tooltip ("Simplify the fin…") sat on top of the "Exact decimal values" option button and hid part of it (screenshot during Q3 of 3, second batch).
- Earlier answer bubbles lose their CHANGE link once a re-asked duplicate comes in (page text shows "Both options" without CHANGE for the first split question) — fine, but means duplicate Qs are tracked separately.

### P3 — 13:27, brainstorm: third re-ask of split mode, ★ default still "Only equal split"
- Saw: after "otherwise looks good, go ahead" the brainstormer reply (13:26 call, `events.jsonl` 4th llm_end) asked split-mode for the third time, again starring "Only equal split". If I'd clicked Build, the hint says unanswered questions take the ★ pick, so the app would have silently dropped the feature I asked for three times.
- Evidence: `.hub/brainstorm.json` last entry; Talk screenshot 13:54.
- Guess: same as P1. Severity raised: combined with the "Build takes ★ picks" rule this can override explicit user answers. Code fix: never re-ask a question whose text/options match an answered one; or the ★ default should be the user's earlier answer.
- (Tester note: I lost ~25 min here by waiting on a model call that had already finished. Not a roughcut bug.)

### P4 — 13:56, round 0 plan: brief decisions re-surfaced as "questions" with wrong options
- Saw: planner turned the three agreed decisions into hub.6–8 ("Split by Percentage or Equal Amounts", "Dark Mode Toggle", "Round to the Nearest Dollar"), status `decided`, shown in Talk as "From the build" cards. hub.6 offered only "Split by Percentage" / "Split by Equal Amounts" — neither means *both*, which is what was agreed; I had to type my own answer. 4th time this topic was asked.
- Evidence: `.hub/questions/hub.6.json`..`hub.8.json`; events.jsonl `question` events ts 1793220962.8.
- Guess: planner prompt emits "decisions" for every brief item; code shows `decided` items as askable. Code: don't surface decisions that restate the brief, or show them as info, not questions. Prompt+code.
- Pointless: 3 more.

### P5 — 13:57, round 0: tests and contract examples written before I could answer the example checks
- Saw: example checks hub.9–12 were non-blocking; `app-bill-js-tests` finished at ts 1793221021 (~60s after they were posted) with `tests/app_bill.test.mjs` asserting `splitBill(115,2,false) → [57.5, 57.5]` and `splitBill(100,3,false) → [33.3, 33.3, 33.4]` — decimals, contradicting the whole-dollar rule in my Talk message. I answered Wrong/[58,57] afterwards.
- Evidence: `tests/app_bill.test.mjs` lines 10–18; `.hub/questions/hub.11.json` (answer "Wrong"), hub.12.
- Guess: code — example checks should block the tests task for that file (or tests rerun once answered). Plus prompt/model: rounding rule not in the brief (P2), so the contract writer never saw it.

### P6 — 13:57, round 0: contract examples use `math.round` (lowercase) — test can never pass
- Saw: contract examples `calculateTotal(100, 15) returns math.round(100 * 1.15)`; the generated "contract examples hold" test inlines this verbatim → `ReferenceError: math is not defined`.
- Evidence: `tests/app_bill.test.mjs` lines 25–26; hub.9/hub.10 (I answered "Right" because the *value* 115/55 is right — the question showed an expression, not a value).
- Guess: model writes expressions in examples; code pastes them unchecked into tests. Code fix: evaluate/validate example expected values (must be a JS literal) when the contract is accepted; or show the user the evaluated value. Code+model.

### P7 — UI: "Wrong" on an example check submits immediately with answer "Wrong" and no way to give the right value in that click
- Saw: clicking Wrong on hub.11 recorded `answer: "Wrong"` and moved on. The "Or your own answer" box is the only way to give a value; easy to miss.
- Evidence: `.hub/questions/hub.11.json`.
- Fix: Wrong should open the input ("what should it return?"). Code, small.

### UI notes (build)
- Coordinate clicks on option buttons sometimes don't register while a round runs (cards re-render every event; refs go stale within ~1s). Screenshots looked stale too. Possibly the Talk list re-renders entirely on each event — which also resets scroll/focus.

### P8 — 14:02, round 0: dark_mode.js stuck; its test is self-contradicting
- Saw: `app-dark-mode-js` failed ("contract can't work as written"). Contract: `toggleDarkMode()` no args, example returns undefined. Test calls `toggleDarkMode(pageElement)` with a fake that has `className` + `toggleAttribute` but no `classList`, and expects `className` to become 'dark-mode'. The contract-example test calls it with no args under node (no `document`). No implementation can pass both.
- Evidence: `tests/app_dark_mode.test.mjs` lines 5–31; `.hub/contracts/app-dark-mode-js.json`; `.hub/questions/app-dark-mode-js.1.json`; task_end ts 1793221333.
- Guess: model (tester writes an ad-hoc fake DOM inconsistent with itself); prompt/code: DOM-touching modules shouldn't get node unit tests at all, or the tester should get a standard fake DOM helper. Planner put DOM work into a logic module. Code+prompt.
- Action: picked Try again (first time, per rules).
- Also: Talk tab has to be active for the stuck-file buttons to be clickable; while on Progress the Talk badge was the only hint a question was waiting.

### P9 — 14:04, round 0: my corrections to example checks were never applied; bill.js stuck on the wrong examples
- Saw: `app-bill-js` failed "contract examples are incorrect… expected [57.5, 57.5]". I had answered hub.11 Wrong and hub.12 "[58, 57]" ~6 min earlier, but the contract is still v1 with the original examples and hub.12 has `acted: false, affects: []`. The builder was correct to complain — it was the contract — but nothing in round 0 acted on the answer.
- Evidence: `.hub/contracts/app-bill-js.json` (version 1, examples unchanged); `.hub/questions/hub.12.json`; `.hub/questions/app-bill-js.1.json`; task_end ts 1793221462.
- Guess: code — answers to example checks are only consumed by the next round's planner (if at all). Should be applied immediately: rewrite the example + regenerate the contract-examples test, then re-run the build task. Code, medium.
- Also the generated code was wrong anyway: equal split `Array(people).fill(Math.round(total/people))` → [58, 58] (sum 116); percentage branch treats the boolean `true` as a percent (`total * (true/100)`). Model.
- Action: Try again (first time for this file).

### Round 0 result (14:05): 4 done, 3 failed (all three logic files stuck); report `.hub/rounds/0.md`
- round_end summary says `"complete": true` with 3 failed tasks and "Tasks ready for the next round: 0" — misleading (events.jsonl round_end ts 1793221502).

### P10 — 14:05, round 1: "Wrong" answers deleted the examples instead of replacing them; `math.round` examples survived
- Saw: app-bill-js contract went v1→v3 ("you said an example was wrong" ×2); both splitBill examples were *removed* rather than replaced with my "[58, 57]". The tester did pick up [58, 57] from somewhere for the plain tests (good). The two `math.round(...)` examples I'd marked Right (value correct) stayed, so the contract-examples test still threw ReferenceError.
- Evidence: `.hub/contracts/app-bill-js.json` history; `tests/app_bill.test.mjs` lines 10–25 (round 1).
- TEMP FIX (project, not roughcut): replaced `math.round(...)` example values with literals 115/55 in `.hub/contracts/app-bill-js.json` + `tests/app_bill.test.mjs`, and 58/57 in `.hub/contracts/app-utils-js.json` + `tests/app_utils.test.mjs` (marked `// TEMP(test-run)` in the tests; JSON can't hold comments).
- Real fix: validate example `returns` as a JS literal at plan time (re-ask the planner or evaluate with node), and show the evaluated value in "Is this right?". Code, small–medium.

### P11 — 14:10, round 1: dark_mode.js stuck again with identical reason; Try again changed nothing
- Saw: `app-dark-mode-js` failed in round 1 with word-for-word the same reason as round 0. Round 1's plan phase took 0.03 s (no planner call), so "Try again" just re-ran the builder against the same impossible test. Two rounds × ~4–5 min of model time burned on it.
- Evidence: events.jsonl phase plan/build ts 1793221503.20/.23; `.hub/questions/app-dark-mode-js.2.json`; task_end ts 1793221836.
- Guess: code — "Try again" when the builder itself said "contract can't work as written" should at least regenerate the tests (or route to the planner). Code, medium.
- Action: Simplify it (second stuck, contract self-contradicting).
- Also round 1: app/utils.js passed (after my TEMP literal fix). It filed a non-question hub `app-utils-js.2` "round up or down when halfway?" with choice "Up or Down" — meaningless; Math.round already decides. Pointless: 1.

### P12 — 14:15, round 1: bill.js auto-split into 4 parts; "split by percentage" has no percentages
- Saw: after the 2nd failure, app-bill-js was split into `app/bill_parts/{calculate_total,split_equally,split_by_percentage,split_bill}.js` (task_end ts 1793222234). New contracts have literal examples (good). But `splitByPercentage(total, people)` takes no per-person percentages and its examples equal `splitEqually`'s, so the "Both options" feature is a duplicate of equal split. The original `splitBill(total, people, splitByPercentage: boolean)` had the same flaw.
- Evidence: `.hub/contracts/app-bill-parts-split-by-percentage-js.json`.
- Guess: planner model (7b) doesn't design data shapes; prompt could ask "what inputs does each feature need from the user?". Model+prompt.
- Real cause of the round-0/1 failure was the model never writing a remainder distribution (`Array(n).fill(Math.round(t/n))` → [58, 58]). Model.

### P13 — 14:21, round 1: split_bill.js fails — parameter shadows the imported function, and the contract forces the name
- Saw: `splitBill(total, people, splitByPercentage)` imports `splitByPercentage` and the boolean parameter of the same name shadows it → `TypeError: splitByPercentage is not a function`. The contract signature from the split names the parameter `splitByPercentage`, so a builder that follows the contract walks into it.
- Evidence: `app/bill_parts/split_bill.js`; contract `.hub/contracts/app-bill-parts-split-bill-js.json` signature; task_end ts 1793222470.
- Guess: code — the splitter/planner could lint contracts for a parameter name equal to an import name (cheap check). Model for not renaming. Code small + model.
- Other round-1 noise: calculate_total builder filed 2 non-blocking "consider adding…" items (cent rounding; default tip not set). Both already decided in Talk. Pointless: 2. The "default tip" one is a symptom of P2 (15% never reached the brief).

### Round 1 result (14:25): 12 done, 2 failed (dark_mode, split_bill), 1 todo. Report `.hub/rounds/1.md`.

### Stop test — 14:26, round 2: works
- Clicked Stop during round 2 build; builder stopped ~2.6 s later ("stopped builder step:app/dark_mode.js partway, because you asked"), round_end `{"stopped": true}`. events.jsonl lines 867–868. Good.

### P14 — 14:26, round 2: "Simplify it" does nothing different from "Try again" (BLOCKER)
- Saw: round 2 notes: "app/dark_mode.js: trying again, because you said: Simplify it" then "unfinished tasks and no new answers, so the plan stays as it is". Contract still v1; the builder went straight back at the same impossible test. The option's own help text promises "The planner trims what this file promises next round".
- Evidence: events.jsonl lines 855–857; `.hub/contracts/app-dark-mode-js.json` history (only v1); `rclocal/rounds.py` `revisit_stuck()` (any answer not in KEEP_ANSWERS → `hub.restart`) and `plan()` (`answered_since(self.number)` excludes answers given *during* the previous round, since `answered_round` = 1 < 2).
- Cause: roughcut code. Two bugs: (a) Simplify has no branch of its own; (b) answers given mid-round are dropped by `answered_since(round)` — this probably also explains P9 (example corrections ignored).
- TEMP FIX (commit 664fb37, `rclocal/rounds.py`, 3 `TEMP(test-run)` markers): revisit_stuck collects Simplify answers into `self.simplify`; plan() appends them to `answers` so the planner runs. Reset `acted` on `.hub/questions/app-dark-mode-js.2.json` by hand so it is picked up. Studio restarted.
- Real fix: make `answered_since` use "answered after round N's plan was made" (timestamp or `answered_round >= N-1` while not acted), and give Simplify an explicit planner instruction naming the file. Medium.
- Also set `.hub/tasks/app-dark-mode-js.json` status back to failed (round 2 had already restarted it to todo), so the patched revisit_stuck sees the answer. Manual project-state edit, TEMP.

### Round 2 (14:28): temp fix confirmed
- Planner ran (no "plan stays as it is"); `app/dark_mode.js` contract v2 (example `toggleDarkMode() → undefined` dropped), `app/bill.js` v5. Two new example checks, both literal and correct ([58, 57], [40, 40, 40]) → Right. events.jsonl lines 871–878.
- UI: the old round-0 example cards still show "calculateTotal(100, 15) returns math.round(...)" in Talk even though the contract now says 115 — history isn't marked superseded.
- UI: studio project list shows SplitTip "2026-10-28 16:25" at 14:26 local — timestamps look 2 h off (timezone?). Small.

### Round 2 result (14:46): 13 done, 1 failed (split_bill.js, 2nd stuck), 1 todo. dark_mode.js now passes (Simplify fix worked).
- split_bill.js failed 3 more attempts on the same shadowing (P13); the builder's own diagnosis names the cause correctly every time but it never renames the parameter.
- round_end `"complete": true` with a failed file, and the run stopped after round 2 of "6 rounds at most" — planner "complete" overrides the failing tests (`.hub/rounds/2.md`, events.jsonl round_end ts 1793223582). Probably should not stop while a build task is failed and unanswered.
- 16:43: Steven said "Try again" in chat for the split_bill stuck question, so I picked Try again (overriding my rule, which would have been It works/Simplify). Clicked Build again.

### Round 3 (16:44): planner "guess" repeats the bug
- hub.19 (planner guess, round 3) proposed literally `splitBill(total, people, splitByPercentage) { return splitByPercentage ? splitByPercentage(total, people) : … }` — the same shadowing bug as a suggested answer. I answered in my own words: rename the parameter to `byPercentage`. Example checks hub.20/21 ([58, 57], [40, 40, 40]) → Right.
- Note: the split_bill tests were rewritten (task_end ts 1793231064) ~15 s after the guess was posted, before I'd answered — P5 again (non-blocking questions race the tests).

### P15 — 16:47, round 3: page check fails — script selects buttons the markup doesn't have
- Saw: page-design failed "after filling in bill=120, tipPercentage=15, people=2, the page threw: Uncaught TypeError: Cannot read properties of null (reading 'addEventListener')". `index.html:104-105` query `[aria-pressed="true"]` / `button[aria-pressed="false"]`, but no element has aria-pressed. Also `app/dark_mode.js` isn't imported by the page at all, so the dark-mode toggle isn't wired.
- Evidence: `index.html` lines 60–105; task_end ts 1793231187.
- Guess: model (designer). Code could help cheaply: the page check could statically list getElementById/querySelector targets missing from the markup and feed that back as the failure reason instead of the runtime error. Also round 2's page-design was marked done "running on placeholders" so the real failure only showed once logic files existed. Model + code (small).

### P16 — 16:48, round 3: the pre-split parent app/bill.js keeps being built and gets stuck, though nothing uses it
- Saw: after the round-1 split into `app/bill_parts/*`, the original `app/bill.js` task stayed alive (todo, then built in round 3) and got stuck ("left for you", app-bill-js.2). The page imports only `bill_parts/`. By hand: `splitBill(115,2,false)` → [58, 58], `splitBill(115,2,true)` → [2, 1]. Dead code eating builder time and asking me a question.
- Evidence: `.hub/questions/app-bill-js.2.json`; `node -e "import('./app/bill.js')…"`; `grep` shows no importer; task_end ts 1793231215.
- Guess: roughcut code — splitting a file should retire (or turn into a re-export shim) the parent contract/task. Code, small–medium.
- Action: rules don't cover "stuck, broken by hand, but unused". GUESSED "I got it" so it neither blocks nor counts as done. Pointless question: 1.
- Round 3 result: 13 done, 2 failed (page-design, app/bill.js). split_bill.js passed after my rename answer. Round 4 started 16:47.
- 16:58, round 4: app/bill.js rebuilt anyway and stuck again (app-bill-js.3, a 2nd pointless question). My round-3 "I got it" (app-bill-js.2) still has `acted: false` — `revisit_stuck()` only handles tasks whose status is `failed`, and this task was `todo` (left over from the split) when round 4 began, so the answer was never applied. Picked "I got it" again.
- Round 4 page-design failed again with the identical aria-pressed null error (task_end ts 1793231344). No progress in round 4 so far.

### Round 4 result (16:55): 13 done, 2 failed — NO PROGRESS (1st no-progress round). page-design still fails; bill.js (dead) still stuck.

### P17 — 16:55, round 5: page script beyond the aria-pressed crash
- Other designer bugs seen in `index.html`: inline `onclick="setSplitByPercentage(true)"` on buttons inside a `type="module"` page (function isn't global → ReferenceError on click); `setSplitByPercentage` re-queries by `aria-pressed="${value}"` (state, not identity); default tip 15% not prefilled (only `data-sample`); zero bill accepted (no refusal of bad input, which I asked for); no dark-mode toggle on the page even though `app/dark_mode.js` passes its tests.
- TEMP FIX (project `index.html`, 3 `TEMP(test-run)` markers, 16:55): ids `splitPercent`/`splitEqual`, listeners by id, no inline onclick, setSplitByPercentage updates by id and recomputes. Applied while round 5's designer is retrying page-design — it may overwrite it.

### P18 — 17:02, end-to-end check in the browser (after my index.html TEMP fix)
- Works: 120 + 15% → total $138; 3 people → $46, $46, $46; 100 + 15% / 2 → $115, $58 / $57; Equal/Percentage buttons toggle with no console errors.
- Doesn't meet the brief: zero bill → "$0, $0", negative bill → "$-57 / $-29, $-29", 0 people silently becomes 1 (`parseInt(...) || 1`) — none refused. Empty form shows "$0" rather than a blank. Tip field empty (15% default not prefilled). Percentage split == equal split (P12). No dark-mode toggle on the page. Page is dark-only, though I asked for light colours on a subtle gradient (designer uses the kit's dark theme; round screenshots in `.hub/rounds/`). "Error" shown as a permanent stat card.
- Evidence: browser tab on the App preview (port 51582), `index.html`.

---

## Final report

### 1. Summary
- **Finished?** Yes, with caveats: the core flow (enter bill, pick tip, split, see shares) works in the browser, but only after a hand fix to `index.html` the designer couldn't make in 3 rounds. Bad-input refusal, the dark-mode toggle, the light look and a real percentage split are missing.
- **Rounds:** 6 (0–5; round 2 was stopped once to test Stop and restarted after a code fix). Two Build presses: "6 rounds at most", then the run stopped itself after round 2 as "complete" with a failing file, and I pressed Build again ("4 rounds at most").
- **Model time:** 3,568 s (~59 min) over 444 calls, none cut off. Builder 2,377 s, designer 519 s, planner 227 s, reviewer 166 s, tester 139 s, brainstormer 86 s, reporter 55 s. qwen2.5-coder:7b for code roles, qwen3:8b for brainstorm/report.
- **Tasks:** 15 at the end: 13 done, 2 failed (`page-design` — hand-fixed afterwards; `app/bill.js` — dead pre-split file, left). Across rounds there were 38 task results: 19 done, 19 failed.
- **Questions put to me:** 29 (9 in the brainstorm, 20 during the build). **Pointless: 11** — 6 brainstorm re-asks of questions already answered, 3 "From the build" restatements of agreed decisions (hub.6–8), 2 stuck-questions about the unused `app/bill.js`. Plus 3 builder "decisions" (round-half-up, cent rounding, default tip) logged but not asked, all already settled.
- **Wall clock:** 13:20 → 17:03, but ~2 h 15 min of that was me not checking back promptly (between round 2's end at 14:46 and 16:43, and ~25 min in the brainstorm). Not roughcut's fault.

### 2. Fix in the software (ranked)

1. **Answers given mid-round are dropped; "Simplify it" == "Try again".** (P14, P9, P16-tail) — *Medium.*
   - Symptom: after "Simplify it", the plan was skipped ("unfinished tasks and no new answers") and the same impossible test was retried. Example corrections and "I got it" answers also had no effect until much later or never.
   - Evidence: events.jsonl lines 855–857; `.hub/questions/app-dark-mode-js.2.json` (`answered_round: 1`); `app-bill-js.2.json` (`acted: false` after a full round).
   - Cause: `HubLocal.answered_since(round)` keeps only `answered_round >= round`, so anything answered during round N is invisible to round N+1's plan; `revisit_stuck()` sends every non-keep answer to `hub.restart` with no Simplify branch, and only looks at tasks in `failed` status.
   - Temp fix: commit 664fb37 (collect Simplify answers, pass them to `plan()`).
   - Real fix: track "acted on by a plan" per answer instead of comparing round numbers; give Simplify its own planner instruction naming the file; apply stuck answers whatever the task's status.

2. **Brainstorm re-asks answered questions and its ★ default overrides the user.** (P1, P3) — *Small–medium.*
   - Symptom: split mode asked 3 times in the brainstorm and once more by the planner, each time starring "Only equal split" after I'd said "Both" every time. Build's "unanswered take the ★ pick" would have silently dropped the feature.
   - Evidence: `.hub/brainstorm.json`; `.hub/questions/hub.1–5.json`.
   - Cause: `Brainstorm.turn()` keeps any question the model returns; `answer()` matches by exact text only.
   - Real fix: drop returned questions whose text or option set matches an answered one (fuzzy, lower-cased), or pre-fill the ★ with the earlier answer; pass answered Q/A to the model as settled facts.

3. **Example checks race the tests and "Wrong" deletes instead of correcting.** (P5, P7, P10) — *Medium.*
   - Symptom: tests were written ~60 s after the "Is this right?" cards appeared, before I could answer, encoding decimals I then rejected. "Wrong" saved the answer "Wrong" with no value and later removed the examples. My typed "[58, 57]" didn't become the example.
   - Evidence: `tests/app_bill.test.mjs` (round 0); `.hub/questions/hub.11.json`; `.hub/contracts/app-bill-js.json` history v1→v3.
   - Real fix: hold a file's tests task until its example checks are answered (or re-generate the tests when an answer lands). Make "Wrong" open the "what should it return?" box, and use a typed value as the new example.

4. **Example values aren't checked for being values.** (P6, P10) — *Small.*
   - Symptom: `returns: "math.round(100 * 1.15)"` was pasted into the contract-examples test → ReferenceError. It stayed for 2 rounds because I answered "Right" (the value 115 *is* right).
   - Evidence: `tests/app_bill.test.mjs:24–25`, `tests/app_utils.test.mjs:17–18` (round 0–1).
   - Cause: `bad_examples()` only re-plans when *more than half* are bad, and `stack.example_ok` accepted the expression.
   - Temp fix: literal values hand-edited into 2 contracts + 2 tests in the project.
   - Real fix: evaluate each `returns` in node at plan time (reject anything that isn't a literal or doesn't evaluate), and show the evaluated value in "Is this right?".

5. **Run stops as "complete" with a failed file.** (Round 2 / 3 results) — *Small.*
   - Symptom: `round_end` `"complete": true` while split_bill.js was failing; the run ended after 3 of 6 rounds.
   - Evidence: events.jsonl round_end ts 1793223582; `.hub/rounds/2.md`.
   - Real fix: don't trust the planner's `complete` while any build task is failed/unanswered or tests fail.

6. **Splitting a file leaves the parent alive.** (P16) — *Small–medium.*
   - Symptom: `app/bill.js` (replaced by `app/bill_parts/*`, imported by nothing) kept being built, got stuck twice and asked me twice.
   - Evidence: `.hub/tasks/app-bill-js.json`; `.hub/questions/app-bill-js.2/3.json`.
   - Real fix: on split, retire the parent task/contract or rewrite it as a re-export shim that passes its tests automatically.

7. **Planner restates the brief as build "questions" with bad options.** (P4) — *Small.*
   - Symptom: hub.6–8 "Split by Percentage or Equal Amounts" with options that excluded "both".
   - Real fix: filter guesses/decisions that match an answered brainstorm question; show decided items as info, not as answer cards.

8. **The brief loses rules from the talk.** (P2) — *Small–medium.*
   - Symptom: the brief kept the "Printable receipt" I'd said to skip (twice) and never got default tip 15%, whole-dollar rounding, "no history" or "refuse bad input". The builder later "suggested" a default tip.
   - Evidence: `.hub/brief.json` nice_to_have[2]; protocol.py BRIEF has no rules/defaults field.
   - Real fix: add `rules` (defaults, rounding, input validation) to the brief schema. Deterministically remove anything the owner said to skip (or confirm the brief with the user before Build).

9. **Page check feedback is too vague, and "done on placeholders" hides failures.** (P15, P17) — *Small.*
   - Symptom: the designer failed 3 rounds on the same null `addEventListener`, rewriting the same code each time, and overwrote my mid-round fix.
   - Real fix: statically diff `getElementById`/`querySelector` targets against the markup and report "no element matches X". Flag inline `onclick` in module pages. Check the page against the real logic files before marking it done.

10. **Contract param name shadows an import.** (P13) — *Small.* A lint at plan/split time: parameter name equals an import name → rename it in the contract. It cost 2 rounds of split_bill.js attempts. The planner's own guess (hub.19) repeated the bug.

11. **Brief and look not enforced in the page.** (P17/P18) — *Medium.* Light/gradient look ignored (dark kit theme). The dark_mode module was built and never wired into the page. Bad-input refusal was never in any contract. These need the brief's look/rules to reach the designer as a checklist, and the page check to test at least one bad input.

12. **UI papercuts.** — *Small each.*
    - Option buttons re-render on every event, so clicks land on stale elements (coordinate clicks were often lost).
    - Stuck-file cards only work with the Talk tab active (a badge is the only hint).
    - A tooltip covered an option button.
    - The project list time was ~2 h ahead (16:25 shown at 14:26).
    - Old superseded example cards (math.round) stay in Talk unmarked.

What worked well: Stop (≈3 s, clean), Change answer, the Progress tab (live output, task table, per-model speed), auto-splitting a stuck file into parts (3 of 4 parts passed first try), and builder diagnoses, which named the real cause every time.

### 3. Model limits (no code change will fully fix)
- **qwen2.5-coder:7b can't write remainder distribution reliably** (`Array(n).fill(Math.round(t/n))` → [58, 58]) until the file is split small. It also never renamed a shadowing parameter even while its own diagnosis named the problem 5 times. Idea: when the builder's diagnosis is "naming conflict"/"contract wrong", route it to the planner rather than another builder try.
- **Testers invent inconsistent fakes for DOM code** (fake with `toggleAttribute` checked via `className`). Idea: don't give DOM-touching modules node unit tests. Or supply a standard tiny fake-DOM helper in the prompt.
- **Planner data design is weak**: "split by percentage" got a boolean flag and no percentages, twice. Idea: a bigger planner model for the planning role only (it's ~6% of model time here), or a prompt step "list the inputs each feature needs from the user".
- **The designer repeats its own broken script** across retries. Idea: include the previous failure with a concrete pointer (see item 9), or use a bigger model for the designer when page-design fails twice.
- **qwen3:8b brainstormer leans to cutting features** ("keep only equal splitting for simplicity") and summarises lossily. Idea: soften the "simplify" instruction once the owner has answered, and tell it the user's explicit "must/nice/skip" lists are fixed.

### 4. TEMP(test-run) changes
| Where | What | Verdict |
| --- | --- | --- |
| roughcut `rclocal/rounds.py` (commit 664fb37, 3 markers) | `self.simplify` list; revisit_stuck collects "Simplify it" answers with a "simplify the contract of X" note; plan() appends them to `answers` | **Rewrite properly** (item 1: fix `answered_since`/acted tracking and give Simplify a real branch) |
| project `.hub/questions/app-dark-mode-js.2.json` | `acted` reset to false by hand | Throw away (state hack for this run) |
| project `.hub/tasks/app-dark-mode-js.json` | status todo → failed by hand | Throw away |
| project `.hub/contracts/app-bill-js.json`, `app-utils-js.json` | `math.round(...)` example values → 115 / 55 / 58 / 57 | Throw away (item 4 prevents it) |
| project `tests/app_bill.test.mjs`, `tests/app_utils.test.mjs` (`// TEMP(test-run)`) | same literal values in the contract-examples tests | Throw away |
| project `index.html` (3 markers) | split buttons get ids, listeners by id, no inline onclick, state set by id | Throw away (model output; item 9 should let the designer get there itself) |

---

# Test run 2 (branch fix-splittip-findings, after the fixes in dff9000)

Started 2026-10-28 17:50. Same studio port, same models, same answers. New project folder (the first run's `u-build-tip` is kept as evidence).

## Log

### Brainstorm (17:52–17:56): 2 turns, 3 questions, 0 repeats
- Turn 1 asked new questions this time (split tip vs total, more than 10 people, Reset button), all fresh. It invented rules "two decimal places" and "max 10 people", but my Talk message replaced them.
- Turn 2 asked nothing and said "The brief looks clear to me". The brief now has `rules`: default 15%, Math.round whole dollars, order doesn't matter, refuse zero/negative bill and zero people. "Printable receipt" sits in out_of_scope (fix 3 worked).
- Miss: I said Yes to a Reset button (hub.3) but it isn't in the brief. The brainstormer dropped it, and no code moves "yes to an add-X question" into the brief. Small.
- Evidence: `~/roughcut-projects/u-build-tip-2/.hub/brief.json`, `brainstorm.json`.

### Round 0 plan (17:57)
- Literal check caught 4 of 11 examples that weren't values and re-asked the planner (events.jsonl ts 1793237424). After the retry, examples that still weren't literal were dropped: `app/bill.js` calculateTotal has none at all now. Better than a ReferenceError, but a file with 0 examples gets weaker tests. Idea: when cleaning leaves an export with no examples, ask only that one again or compute the value with node.
- Example checks (hub.7 `splitAmount(115, 3) → [38, 38, 39]`, hub.8) were correct. I answered within ~30 s, and `app-split-js-tests` didn't start until both were answered, while the other tests tasks ran (fix 4 worked).
- MISS: 3 planner guesses got past the repeat filter ("Rounding Rule", "Tip Calculation Method", "Split Method"). Their **picks** restate the brief ("Round to the Nearest Dollar"), but the titles are too short to match on text alone: "Rounding Rule" overlaps the rule by 1 word out of 2. Fix: compare text + choice against rules and answers. Pointless questions: 3.

### Round 0 result (18:57, ~60 min): 12 done, 3 failed, 2 todo
- Done first try or close: tip.js, bill.js, utils.js, all tests tasks. The builder-decision filter dropped 5 repeats ("Default tip percentage", "Rounding rule" ×2, "default 15% configurable?"). One still got through as a non-card decision ("tip rounded to whole number, configurable?").
- page-design failed 3 drafts: twice for importing an export from the wrong file (tip.js / split.js don't export calculateTotal, bill.js does), then the draft got cut off at the length limit. The designer prompt lists contracts, but the 7b model mixes up which file exports what.
- split.js: failed, auto-split into 4 parts (`app/split_parts/*`). calculate_total piece passes.
  - validate_input.js stuck: contract blamed (the test expects a thrown object with `.message`, the code throws an Error; that's really a test bug). First stuck → Try again → **"back to the planner, with new tests"** (fix 11 worked, events.jsonl round 1 start).
  - split_amount.js stuck: the planner made it call calculateTotal (adds the tip) when it should split an already-tipped total. Contract/data-flow confusion. First stuck → Try again.
- Questions so far: 3 brainstorm, 5 plan (3 pointless guesses + 2 example checks), 2 stuck = 10. Pointless 3.

### Round 1 plan (18:58–19:00)
- The literal check rejected 3 of 5 examples again and re-asked. The second plan's examples were all literal and correct (hub.11–14: splitAmount(120, 4) → [30×4], calculateTotal(100, 15) → 115, (115, 15) → 132, (100, 0) → 100). All Right.
- Planner calls in round 1: 66 s + 91 s. No hang (I misread a timestamp; corrected the round 0 time above).

### Round 1 (19:05): my unused-file check blocked the page — fixed mid-run
- page-design failed 3 drafts with "the page never uses app/split.js, app/tip.js". The plan has two files that compute the total (tip.js and bill.js), so a page that uses one is fine; the check was too strict (fix 14C, my own regression). Changed it to a note emitted when the page passes (commit after dff9000), and the studio restarts after round 1.

### Round 1 (19:15): validate_input.js stuck again — reviewer blamed the contract, but it's the code
- Code: `throw new Error({ message: 'Bill must be a positive number.' })` → message "[object Object]". The tests (`assert.throws(..., { message: '...' })`) are right. The reviewer (qwen3:8b) said "contract can't work" twice, so Try again sent it to the planner, which couldn't fix a code bug.
- Evidence: `app/split_parts/validate_input.js`; `node -e` by hand: throws "[object Object]"; `.hub/questions/app-split-parts-validate-input-js.2.json`.
- GUESS: the rules didn't fit (it doesn't work by hand, and the contract isn't impossible). I answered in my own words with the exact fix, which restarts the builder with my text as a note.
- Software idea: when the review blames the contract, run a cheap check first — does the failing assertion's expected value appear in the contract? If the contract and test agree, blame the code.
- 19:18, calculate_total.js (split piece) stuck: here the **tests** are wrong — they expect `calculateTotal(-100, 15)` → -100 and `(0, 15)` → 0, against the brief's rule "refuse a zero or negative bill". The code (throws) follows the rule. The tester ignored the rules that reach it. First stuck → Try again (by the rules; "It works" would be the honest answer).
- Pattern in this run: the reviewer's blame is unreliable — once it blamed the contract for a code bug, once the code for a test bug. That decides where the fix goes, so it matters more than any single prompt. Idea: a deterministic tie-break — if the failing assert's expected value contradicts a brief rule or a contract example, blame the test; if the test matches the contract, blame the code.

### Round 2 (19:20–19:30)
- Page passes its check for the first time this run ("index.html and styles.css work in the browser"), after the unused-file check became a note.
- validate_input.js examples (hub.16–18, valid input → undefined) correct → Right.
- calculate_total.js (piece) stuck 2nd time: blamed the contract for "expects a default export". Actually the **tester** wrote `import calculateTotal from …` (a default import) though the contract has a named export. By hand it returns the right totals (115, 132, 100). For a bad bill it returns the bill instead of refusing (it followed round 1's wrong tests), but validate_input.js does the refusing. Picked **It works** (by the rules).
- Software fix: `repair_test` could rewrite `import X from '../app/f.js'` to `import { X } from …` when X is a named export in the contract. Cheap and deterministic; this exact mistake cost a full builder attempt plus a review.
- 19:32, validate_input.js stuck a 3rd time. My round-1 hint fixed the code (`throw new Error('Bill must be a positive number.')`), but the regenerated tests (contract v3) use `t.throwsAsync(...)`, an AVA API that node:test doesn't have → TypeError in every test. Reviewer again said "contract can't work". By hand it throws the right messages and returns undefined for good input → **It works**.
- Software fix: `test_problem` should reject test files that call APIs node:test/node:assert don't have (`t.throws*`, `t.is`, `expect(`, `jest.`), the same way it rejects missing imports. Deterministic, small.

### Round 3 (19:34)
- 3 new example checks for calculate_total, 2 against the brief's rule: `(0, 15) → 0` and `(-50, 15) → 0` should be refused → Wrong ("Just drop it"); `(100, 0) → 0` → Wrong, typed 100. The new Wrong flow worked (it asks for the value, with a "Just drop it" button), and the drop applied mid-round ("calculateTotal(0, 15) is dropped, as you said", fix 4B/5).
- The planner keeps writing examples that break the brief's "refuse" rule for calculate_total, even though `rules` reach it. Model limit; or the refusal belongs in validate_input only and the planner is right that calculateTotal never sees bad input. Ambiguous ownership.

### Round 3 result (19:51): no progress (12 done) — stopped round 4 to fix roughcut
- "It works" didn't stick. calculate_total.js was marked done at round 3 start, but the stuck question's text names the file, so `protected()` dropped its protection and the round 3 plan rewrote its contract (v4). That made new example checks, then a rebuild, and it got stuck the same way again. validate_input.js's "It works" was given ~10 s after round 3 started, so the round's `revisit_stuck` missed it and `retry_failed` rebuilt it.
- Both are regressions or gaps in my fix 1. Fixed in 9212d80: stuck-file answers are kept out of plans and protection (`is_stuck_question`), and they're applied between tasks mid-round as well as at round start.
- Also fixed (deterministic): `repair_test` turns `import X from '../app/f.js'` into `import { X }` when X is a named export; `test_problem` rejects AVA/Jest APIs (`t.throwsAsync`, `t.is`, `expect(`). Those two tester mistakes caused 4 of this run's stuck questions.
- Guess filter now also checks the guess's pick ("Round to the Nearest Dollar" matches the rule).
- Rounds without progress so far: 1 (round 1), round 3. Round 2 made progress (page).

### Round 4 (19:55–20:09)
- Round start: "calculate_total.js: done, because you said it works as it is" and the same for validate_input.js. Both stayed done through the plan this time (9212d80 worked).
- Page passed again. split_logic.js stuck: it returns `splitAmount(115, 3)` → [39, 38, 38], the contract says [38, 38, 39]. The brief's rule says order doesn't matter, but `deepStrictEqual` does. First stuck → Try again (by the rules).
- Software idea: when the brief says order doesn't matter (or for any whole-dollar split), the contract-examples test could compare sorted arrays. Better in general: let a contract example carry a "same items, any order" flag the planner sets, and have `example_tests` emit `assert.deepStrictEqual([...x].sort(), [...].sort())`.
- 20:11, bill.js (done since round 0) was rewritten by the round 4 plan and then got stuck: its new code returns 0 for `calculateTotal(100, 0)` (should be 100), and the reviewer blamed the contract (wrong again). Cause: my round 3 answer "calculateTotal(100, 0) → 100" was about `app/split_parts/calculate_total.js`, but `protected()` matches answers to files by export name, and bill.js also exports calculateTotal. So bill.js lost its protection. Fixed (commit after 9212d80): an example answer only counts for its own file. Picked Try again (first stuck for bill.js).
- Wider point: two files exporting the same function (bill.js and split_parts/calculate_total.js both have calculateTotal) is a planning smell. The split made a duplicate instead of importing the existing one.
- 20:19, split_logic.js stuck 2nd time, on the order again (the tests were rewritten but still expect a fixed order). By hand: (115, 3) → [39, 38, 38], (99, 2) → [50, 49], (100, 5) → [20×5], (120, 4) → [30×4], all right and adding up to the total → **It works**.
- 20:20, bill.js stuck 2nd time: code `if (bill <= 0 || tipPercentage <= 0) return 0;` → (100, 0) gives 0. The contract (→ 100) is right; the reviewer blamed the contract both times. GUESS (rules don't cover "broken by hand, contract fine"): answered with the fix in my own words.
- 20:25, split_amount.js stuck 2nd time on array order; by hand all right → **It works**.
- 20:28, bill.js stuck 3rd time; my hint didn't change the code (`tipPercentage <= 0 → return 0` stayed). Nothing imports bill.js: the page uses `split_parts/calculate_total.js`, a duplicate made by the split. GUESS: **I got it** (unused file, same call as run 1).

### Round 4 result: 15 done, 2 failed (split.js parent; bill.js parked). Round 5 stopped at once: the app works.

### End-to-end check in the browser (no hand edits this run)
- 120 + 15% for 3 → total $138.00, share $46.00. 100 + 15% for 2 → $115.00, $58.00. A zero bill, a negative bill and 0 people are refused (results go back to "—"). No console errors.
- Gaps against the brief: one "Individual Share" figure instead of each person's share (for 115 / 2 it shows $58, not $58 and $57); shown with cents ($46.00) though splits are whole dollars; no split-by-percentage; no dark-mode toggle; dark kit theme instead of light-on-gradient; 15% isn't prefilled; refusals show no message, just "—". The page uses `split_parts/*` directly, so `split.js`, `bill.js` and `tip.js` are built but unused.

---

## Run 2 summary

| | Run 1 | Run 2 |
| --- | --- | --- |
| Works end to end | only after a hand fix to index.html | **yes, no hand edits to generated code** |
| Rounds | 6 | 6 (round 4 stopped once for a fix, round 5 stopped when the app worked) |
| Model time | 59 min, 444 calls | 96 min, 556 calls (builder 62 min, planner 13, designer 9) |
| Tasks at end | 13 done / 2 failed | 14 done / 1 failed / 2 todo (the 3 not done are unused files) |
| Questions to me | 29, 11 pointless | ~33: 3 brainstorm, 3 plan guesses, 12 example checks, 15 stuck-file. 3 pointless guesses; ~4 more stuck questions came from roughcut bugs fixed mid-run |
| Repeat questions | 6 brainstorm + 3 restated | 0 brainstorm; 3 plan guesses (fixed after); 9 repeats filtered automatically |
| Brief | lost rules, kept skipped receipt | all rules kept, receipt in "left out" |
| TEMP edits to generated code | 3 files | none |

Model time went up because the split pieces needed many attempts. Most failures traced to two causes: tests holding arrays to one order the rules say doesn't matter (3 files, 6 stuck questions), and the reviewer blaming the contract for code or test bugs (5 of 7 "contract can't work" verdicts were wrong).

## Code changes during run 2 (all real fixes, no TEMP markers)
- `dff9000` — the recommended fixes from run 1 (see the list in chat).
- Page check: a logic file the page doesn't use is a note, not a rejection (my unused-file check had blocked the page in round 1).
- `9212d80` — stuck-file answers ("It works", "I got it") are kept out of plans and file protection and are applied mid-round; `repair_test` turns default imports of named exports into named imports; `test_problem` rejects AVA/Jest APIs; guesses are also matched on their pick.
- Next commit — an example answer only unprotects its own file, not every file with an export of the same name.
- Studio: Wrong asks for the value, the question card stays put, a note shows waiting questions on other tabs, tooltips sit above options, and a falling-block game for while you wait.

## Still to fix (ranked, from run 2)
1. **Reviewer blame is unreliable** (M). 5 of 7 contract verdicts were wrong, and the verdict decides where the fix goes. Add a deterministic tie-break: if the test's expected value equals the contract example, blame the code; if it contradicts a brief rule, blame the test.
2. **Order-insensitive examples** (S–M). Let the planner mark an example "any order", or apply it automatically when the brief says order doesn't matter, and emit sorted comparisons. This would have saved ~6 stuck questions.
3. **Duplicate functions across files** (M). The split made `split_parts/calculate_total.js` beside the existing `bill.js` calculateTotal, and the page used neither parent. Split pieces should import existing contracts rather than redefine them; flag exports with the same name in two files.
4. **The page doesn't match the brief** (M). It shows one share instead of each person's, uses cents, has no toggle or percentage split, and the theme is dark. The designer needs the brief's must-haves as a checklist the page check tests (e.g. "results show N values for N people").
5. **Examples dropped to none** (S). After the literal retry, calculateTotal in bill.js kept 0 examples. Re-ask for that export only, or evaluate the call with node to fill in the value.
6. **Yes-to-add answers don't reach the brief** (S). The Reset button I said yes to was lost.
7. **A file's tests rewritten to contradict the rules** (model). The tester ignored "refuse bad bills" twice.

## What other harnesses do that roughcut could borrow (beyond "a bigger model")
- **Agentless** (localize → repair → validate): one model call per stage, no free-form agent loop, and *several candidate patches ranked by tests* (reproduction + regression tests). For roughcut: on a file's last attempt, sample 2–3 whole-file candidates at a higher temperature in parallel and keep the one that passes the most tests. Small models are much better at "one of three is right" than "the first one is right".
- **AlphaCodium** ("flow engineering"): reason about the problem in plain words first, then generate code against public tests plus *AI-generated tests*, with "test anchors": tests that already passed must keep passing, and they "always fix the code, not the tests". For roughcut: a one-paragraph "what this file must do, and its edge cases" pre-step for the builder, and anchors. Once a test passes for a file, a later rewrite of that file's tests may not drop it. That would have stopped calculate_total's tests flipping from "refuse" to "return the bill".
- **Aider**: lint and test after every edit, feed the failure straight back, and use *whole-file edits for weaker models* (roughcut already does both). Its repo map (signatures only, from tree-sitter) is the same idea as roughcut's contracts.
- **SWE-agent's "agent-computer interface"**: the tools reject a bad edit (a linter guard) before it lands. Roughcut's `check_code` is this; this session added parameter shadowing, page access in logic files and other runners' test APIs. More cheap linters (unused import, duplicate export names across files, `new Error({...})`) are the best value per line of code.
- **Deterministic before generative**: every fix in this session that worked best was code, not a prompt (literal check, re-export shim, repeat-question filter, import repair). The general rule: whenever a small model's mistake is recognisable by a regex or by running node, catch it in code and either repair it or send back a one-line reason.

Sources: Agentless — github.com/OpenAutoCoder/Agentless (via newfacade.github.io/notes-on-llm/agent/agentless.html); AlphaCodium — arxiv.org/abs/2401.08500; Aider — aider.chat/docs/usage/lint-test.html.

## Replay: the blame checks on runs 1 and 2 (no model calls)

`tools/replay_blame.py` unpacks each round's build commit, reruns the tests of every file the reviewer judged that round, and puts the facts' verdict (`rclocal/blame.py`) beside the reviewer's. 22 (round, file) cases; the code is the last version of that round, so a file reviewed twice in a round is replayed once. Truth is from the notes above, or by hand.

| Case | Reviewer | Facts | Really wrong |
|---|---|---|---|
| run 2 r0 split.js | code | code | code ✓ |
| run 2 r0 split_amount.js | code | code | code: it adds the tip again; the examples pin the right numbers ✓ |
| run 2 r0, r1, r2, r3 validate_input.js | contract ×4 | reviewer ×4 | test (r0, r3: `.message` object, `t.throwsAsync`), code (r1) |
| run 2 r1 calculate_total.js | code | test | test: expects -100 for a bill the rules refuse ✓ |
| run 2 r2, r3 calculate_total.js | contract ×2 | test ×2 | test: default import ✓ |
| run 2 r4 bill.js | contract | code | code: `(100, 0)` gave 0, the example says 100 ✓ |
| run 2 r4 split_logic, split_amount, split.js | contract ×3 | code ×3 | arguable: the code was right in any order, the examples fix one order; "code" asks it to match the order, which a builder can do |
| run 1 r1, r2 split_bill.js | code ×2 | code ×2 | code: a parameter hid the import ✓ |
| run 1 r4 bill.js | code | code | code ✓ |
| run 1 r1 bill.js | code | reviewer | code (splitBill had no examples, so the facts don't decide) |
| run 1 r0 bill.js, utils.js | contract ×2 | code ×2 | contract: examples like `math.round(...)`. Can't happen now: such examples never reach the tests |
| run 1 r0, r1 dark_mode.js | contract ×2 | code ×2 | contract: a logic file that needs the page. Now refused at plan and build time |
| run 1 r2 dark_mode.js | code | (passes at the build commit) | — |

- **Run 2 (today's pipeline): the facts settled 9 of 13 and got none wrong** (5 right, 4 arguable order cases). The reviewer got 8 of the same 13 wrong. The 4 left to the reviewer are all validate_input.js, where something threw; that's the next thing to look at (a thrown non-Error, a test API node doesn't have).
- **Run 1:** the 4 cases the facts get wrong all come from contract mistakes the current checks stop earlier (non-value examples, a logic file that needs the page). So a wrong contract example still makes the facts blame the code; your example checks are what protect against that.
- Two rules were tightened by the replay: an example that isn't a value doesn't count against a test, and "the code meets every example" only blames the tests when every function has usable examples (run 1 r1 bill.js would otherwise have blamed the tests for a real code bug).
