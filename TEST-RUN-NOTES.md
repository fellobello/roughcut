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
