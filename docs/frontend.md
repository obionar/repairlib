# Web frontend map

Server-rendered Jinja2, one stylesheet, no JavaScript required. The browser app is
a FastAPI adapter under `src/repairlib/adapters/web/`:

```
adapters/web/
  app.py          router assembly (in app.py at src/repairlib/app.py)
  routes.py       shell, sign-in, dashboard, setup
  customers.py  repairs.py  inventory.py  settings.py  system.py  ...
  templates/      base.html, partials (_*.html), one file per screen
  static/app.css  the only stylesheet
  static/*.svg    icons referenced by templates
```

## Templates

| File | Role |
|---|---|
| `base.html` | Document shell: masthead, navigation, main, skip link, page header |
| `_shell.html` | Macros: `brand`, `link`, `icon` |
| `_ui.html` | Macros: `panel`, `table`, `pager`, `search`, `search_input`, `select_filter`, `button`, `action`, `status`, `notice`, `empty`, `local_time` |
| `_repair_rows.html` | The repair queue table, shared by dashboard and lists |
| `_repair_activity.html` | Folded activity projection for a repair |
| `_settings_nav.html` | Settings section navigation |
| `customers.html` | Customer list, create and edit form |
| `customer_detail.html` | Customer record: contact header, repairs workspace, supporting history |
| `repairs.html` | Repair register with filters |
| `repair_form.html` | Repair intake and edit: customer, device and problem; existing assignments are preserved but not shown |
| `repair_detail.html` | Repair workspace shell: record header, error summary, window includes |
| `_repair_status.html`, `_repair_log.html`, `_repair_stock.html`, `_repair_fees.html`, `_repair_files.html` | One titled window each; `_repair_pickup.html` supplies the pickup control in the status row; `_repair_stock.html` is the Parts used window (catalog search, manual part, used list), `_repair_fees.html` the Payment window |
| `inventory.html`, `inventory_detail.html` | Catalog register and item record |
| `users.html`, `settings.html`, `account.html`, `tokens.html`, `system.html` | Administration |
| `charge_form.html`, `consume_part.html`, `return_part.html` | Repair sub-screens |
| `print_summary.html` | Printable handover (print stylesheet, no chrome) |
| `error.html` | Refusal and error pages |

## Conventions

- One screen per template. Shared markup lives in a macro in `_ui.html` or a
  `_*.html` partial; do not copy a field loop, table or panel between screens.
- Titled windows (`ui.panel`) sit directly on the workspace canvas. The customer
  page stacks contact details and repair count, repairs, then history at every
  width. The ticket header lists customer and device facts on the left and the
  reported issue and intake notes on the right (stacked on phones), with the
  status control at the bottom of the panel. Below it, the work log (a chat:
  newest entry last, composer underneath) and Activity share the left column;
  parts, payment and files share the right.
  Browser reopening does not require a note. The dropdown shows the
  current status itself, keeps the full list in workflow order, and disables
  unavailable transitions. Repair, pickup and payment share one Update status
  button. Pickup and payment stay visible but disabled until the ticket is
  finished. Once finished, a Hand over button marks the repair paid and picked
  up in one step and disappears when both are recorded; the dropdowns stay for
  corrections. Payment records a status, not an amount. Print sits after Edit
  details. Parts used comes first in the markup so phones reach the quick-add
  form before the log; on wide screens the two panels sit in equal columns with
  Work log on the left. Print label sits beside Print in the header. Files keeps
  the attach form behind a disclosure. Parts used starts with a catalog search
  (`?part_q=`, results inline, each with a quantity and Use button posting to the
  catalog consume route); a miss opens the manual "Part not in the catalog" form with
  the search text as its name. Browse catalog keeps the full picker. Either adds
  one `Part used: …` entry and a charge; only catalog use moves stock. The old
  Charges window and discount form are gone from the ticket; the API and CLI keep
  those contracts. Payment holds parts (read-only), labour, discount and total.
  Device and problem fields
  keep visible labels in their edit form. Audit history stays folded initially.
  Warranty fields are not part of the day-to-day ticket UI. Estimates were removed
  everywhere at schema 36.
- Forms: native `method="post"`, one primary submit, optional cancel link. Fields
  sit in `.obi-intake-grid`; each control keeps a visually hidden `<label>` and a
  visible placeholder. Long text fields add `class="obi-span"`.
- Every page renders one semantic `h1`; authenticated pages hide it visually when
  the content already starts with titled windows.
- Keep permission, CSRF, revision, idempotency-key and error markup exactly as the
  handlers expect. Styling never changes names, ids or routes.
- Core forms work without JavaScript. The optional `page-scroll.js` (repair, inventory item and customer pages)
  keeps the scroll position after any form or pager on the page; redirects and
  back links carry no `#panel` fragment, so without JavaScript the page reloads at the top.
  It also submits the device photo as soon as a file is chosen; without it, an Upload button
  appears inside the photo frame once a file is picked. If
  editing the script, update both its template integrity hash and the CSP hash
  in `app.py`. No external fonts, icon packs or CSS frameworks.

## Stylesheet sections

`static/app.css` is one file read top to bottom: tokens and base, shell, workspace
and page header, windows, tables and registers, search, forms, record screens,
feedback, then print last. Add a rule to the section that owns it and keep the
print block at the end.

## Automation hooks

Regions carry stable ids instead of test-only attributes; treat them as a
contract and update this table when one changes.

| Hook | Where |
|---|---|
| `#primary-navigation` | Main navigation |
| `#main` | Main landmark |
| `#open-work`, `#awaiting-pickup`, `#low-stock`, `#recent-customers` | Dashboard windows |
| `#customer-search`, `#customer-search-title`, `#customer-results` | Customer list |
| `#customer-form-details`, `#customer-record`, `#customer-repairs`, `#customer-history` | Customer screens |
| `#repair-search`, `#repair-search-title`, `#repair-results` | Repair register |
| `#repair-form-customer`, `#repair-form-device`, `#repair-form-problem` | Repair form |
| `#status`, `#device-details`, `#log`, `#payment-total`, `#fees-form`, `#stock-usage`, `#manual-part-form`, `#files`, `#pickup`, `#activity`, `#stage-form` | Repair workspace |
| `#inventory-results`, `#user-results` | Administration registers |
| `#form-error`, `#error-message` | Refusal and validation messages |

Forms submit with the browser's own controls, so a Playwright or CDP session only
needs to fill named inputs (`name="q"`, `name="stage"`, ...) and press the submit
button; no waiting for hydration and no framework state.

## Verification

Render with a disposable workspace: sign in, then check the record and register
screens at 390px and 1280px with JavaScript disabled. `docs/workshop.md` describes
the expected workflow of every screen.

`scripts/ui_smoke.py` automates that pass over HTTP: it signs in through the form,
keeps JavaScript off, checks stable hooks on sixteen screens, and verifies the
stacked record layout, visible status editor, and keyboard folding.

```sh
python3 scripts/ui_smoke.py --password-file data/.initial-admin-password --width 1280
```

It needs Playwright and a Chromium binary (`CHROMIUM` env, default
`/usr/bin/chromium`); it is a development check, not part of the package.
