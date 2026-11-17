# Presentation refresh — 2026-11-14

The owner requested a visual direction like [SalonLog](https://salonlog.com/).
Its public page and stylesheet were inspected: purple accents, pale lilac and
cream surfaces, straightforward language and a phone-focused presentation.
NCRFlow's templates and CSS are original. No SalonLog branding, images, business
copy or source styles were copied.

## Scope

- `static/css/app.css`: shared purple palette, readable headings, white rounded
  cards, larger controls, clearer status/error styling and responsive layouts.
- `templates/base.html`: wordmark, active navigation, existing role-specific
  links and a mobile bottom bar for Workspace, NCRs and Raise NCR.
- `templates/home.html`: original welcome copy, an explicitly illustrative NCR
  phone preview, and a three-step explanation of the existing internal workflow.
- `templates/accounts/login.html`: lilac welcome panel and a clearer sign-in card.
- `templates/tenants/workspace.html`: action cards for existing features and
  a compact account summary, with Quality and Company-admin links scoped as before.

There are no new product features, data queries, permissions or authentication
changes. Existing routes, form actions, POST logout, CSRF tokens and plain audit
history remain in place. The public phone illustration is static example content,
not customer data or a dashboard. Signup, billing and analytics remain later work.
No external fonts, image assets or JavaScript dependencies were added.

## Responsive and accessibility choices

Layouts stack below 720px; the header wraps, tables scroll in their containers,
and authenticated members receive labelled bottom navigation with safe-area
padding. Text labels accompany navigation symbols. Buttons and form controls
have 44–48px minimum heights. Keyboard focus and the skip link remain visible;
no animation is required for navigation. Status and errors retain text labels.

## Migration setup correction found during the code check

The working tree already contained the Phase 3 NCR supplier model/migrations.
Migration0004 failed during SQLite table rebuild because an audit trigger on a
different table referenced the temporarily missing NCR table. Local PostgreSQL
history confirmed0004/0005 were unapplied before the correction.

The repair suspends only that cross-table scope trigger during rebuilds and
restores the identical guard plus the existing report guards afterward, in both
directions. PostgreSQL behaviour is unchanged. The migration regression now tests
latest →0003 →latest with raw foreign-company audit insertion, report identity
and retention checks, as well as the older deeper rollback/reapply coverage.
The approved model change log records this technical correction.

## Validation

Independent review: PASS, 0 Blockers, 0 Should fix, 0 Minor. The reviewer checked
the UI boundaries and migration repair and independently passed the migration
roundtrip and 46 login tests.

Full suite: **961 passed in 111.78s**. Django check, migration drift, Ruff lint
and formatting checks pass. The migration regression passed again after its
line-wrapping correction. Local PostgreSQL migrations suppliers0001/0002 and
ncr0004/0005 applied successfully. Live HTTP checks returned200 for the welcome,
sign-in and CSS endpoints, then verified workspace navigation and NCR list/detail
for all four demo roles, plus the Company-admin supplier list. These HTTP checks
changed no NCR/company business data. No new presentation tests were added for
the CSS-only behaviour; existing role/login and workflow tests provide regressions.

Interactive screenshots, browser layout, keyboard use and screen-reader behaviour
have not been verified; browser inspection was unavailable. Source/CSS review and
HTTP checks must not be described as a completed visual browser review.

Try the running app by refreshing the welcome page, signing in, and opening the
workspace and NCR screens. Check the same screens on a phone and with keyboard
navigation. Keep Docker running and use `python manage.py runserver` from the
inner project directory if the development server is stopped.
