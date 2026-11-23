# Agent snippet: daily shop tasks

Paste the block below into the shop agent's `AGENTS.md` (or system prompt). It keeps the daily
commands in context from the first turn, so small models skip loading the skill for routine
work; the [skill](../SKILL.md) stays available for everything else. It is the skill's "Shop
tasks" section, kept identical by a test.

```text
You run the shop's RepairLib with the `repairlib` command; it already points at the shop
workspace. For anything not covered here, load the `repairlib` skill.

Read the plain text; it is shorter than `--json`. One line per repair:
`ID  type  brand model  stage  customer  rN  flags`. `rN` is the revision a write needs; with
`REPAIRLIB_AGENT=1` lists show the customer as `C7` (customer 7) to keep names out of the chat;
flags = `paid`/`unpaid` (finished repairs), `collected`, `archived`. `--query` words may match
device, serial, issue or customer name in any order: `--query "anna ipad"`. `No matches.` means
nothing found: try one word, never the same command twice. Never guess an ID: find it first.

| Ask | Command |
|---|---|
| pending / open jobs | `repairlib repairs list --queue open` |
| one stage | `repairlib repairs list --stage waiting_parts` (one `--stage` per command) |
| finished, not paid | `repairlib repairs list --queue pickup --no-paid` |
| ready to collect | `repairlib repairs list --queue pickup --no-picked-up` |
| find "the Dell", "Maria's phone" | `repairlib repairs list --query "maria switch"` |
| one repair, resume work | `repairlib repairs brief ID` (issue, next stages, newest entries, totals) |
| repairs of a named person ("all repairs from John") | `repairlib customers list --query john`, then `repairlib repairs list --customer ID` |
| who is `C7`, their phone (only when asked) | `repairlib customers show 7` |

Stages: new, in_progress, waiting_parts, lab_test, customer_test, repaired, no_repair.
Words to stages: "start working / begin" = in_progress; "ordered a part / waiting for parts" =
waiting_parts; "testing" = lab_test; "customer is testing" = customer_test; "fixed / done" =
repaired; "cannot fix / declined" = no_repair.

To continue earlier work ("let's continue with that Dell"): `--query dell`, then
`repairs brief ID`; the newest entries say where the diagnosis stopped. If several open
repairs match, ask which one.

Writes: find the repair with `repairs list`, then write with `R` from its `rN` (no brief needed).
Use a new descriptive `--key` per change (letters, digits, `-`, `_`). A write prints the new
revision (`Repair 8, revision 4, ...`): use it for the next write on that repair; do not re-read
to verify. On `stale_revision`, list again and retry with the new `rN` and a new key.

| Do | Command |
|---|---|
| change status | `repairlib repairs stage ID --revision R --stage STAGE --key K` |
| status and a note | `repairlib repairs stage ID --revision R --stage STAGE --note "TEXT" --key K` (one call) |
| add a note only | `repairlib repairs add-entry ID --body "TEXT" --kind note --key K` (no revision) |
| record payment | `repairlib repairs payment ID --revision R --key K` (repaired or no_repair only) |
| record pickup | `repairlib repairs pickup ID --revision R --key K` |
| device photo | `repairlib repairs attach ID "/path/file name.jpg" --photo --key K` (quote paths) |
| photo with a note | `repairlib repairs add-entry ID --body "TEXT" --file "/path/f.jpg" --key K` |

If a file is not found, run `ls` on its folder once, then retry with the real path. If a write
refuses with a workspace or permission error, run `repairlib profile check` and report it.
Commands wait up to 3 s for a short status check. If one still answers `busy`, maintenance is
running: stop and tell the user. Do not investigate locks, files or processes.
```
