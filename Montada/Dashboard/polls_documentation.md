# Scheduled Polls

Business logic: `Dashboard/polls.py` (single source of rules for views, admin API and scheduler).
Models: `Dashboard/models.py`. Admin views: `MontadaAdmin/views.py`. Scheduler: `update_poll_statuses`.

## Concepts

| Term | Meaning |
|------|---------|
| Poll | `PollQuestion`. Its `id`, options and votes are stable across resets. |
| Session | `PollSession`: one voting period with `start_at`, `end_at`, `status`, numbered 1, 2, 3… per poll. `PollQuestion.current_session` is the latest. |
| Snapshot | `PollSessionOption`: the option texts offered in a session. Later option edits never change a past session's results. |
| Vote | `PollResponse`: belongs to exactly one session. Unique per `(user, session, option)`; one ballot per user per session. |

**Statuses** (per session): `scheduled` → `active` → `closed`, or `cancelled`.

- A future `start_at` makes a session `scheduled`; it becomes `active` at `start_at` and `closed` at `end_at`.
- `closed` and `cancelled` are never reopened by the scheduler. An admin can reopen a *closed* session
  explicitly (`/activate/` or `/close/` with `reopen`); a *cancelled* one can only be followed by a reset.
- Voting and listing compare against the schedule directly (`effective_status`), so they are correct even if
  the scheduler is late. The scheduler keeps the stored `status` and `PollQuestion.is_active` up to date.

**Defaults:** a new poll or reset session starts now and lasts the poll's `duration_hours` (default 168 = 7 days)
unless `start_at`/`end_at`/`duration_days`/`duration_hours` are given. All datetimes are ISO 8601; naive values
are interpreted in `TIME_ZONE` (UTC). Responses are UTC ISO strings.

**History is never deleted:** reset closes the old session and keeps its votes; removed options are
soft-deleted (`is_deleted`); deleting a poll that has votes hides it (`is_deleted`) and keeps its sessions.

## User API (unchanged contract)

The Flutter app needs no changes. Request and response shapes and error messages are the same as before.

### GET `/api/dashboard/polls/active/`

Returns only polls whose current session is active. `vote_count`, `vote_percentage` and `is_voted` cover the
**current session only**, so after a reset they start again from 0 / `false`.

```json
{
  "polls": [
    {
      "questions": [
        {
          "id": "3f6c…",
          "question_text": "Where is EURUSD heading this week?",
          "question_type": "single",
          "order": 0,
          "options": [
            {"id": "a1…", "option_text": "Up", "vote_count": 2, "vote_percentage": 66.67},
            {"id": "b2…", "option_text": "Down", "vote_count": 1, "vote_percentage": 33.33}
          ],
          "is_voted": true
        }
      ]
    }
  ]
}
```

### POST `/api/dashboard/polls/vote/`

```json
{"poll_id": "any", "question_id": "3f6c…", "option_ids": ["a1…"]}
```

`201 {"message": "Vote recorded successfully."}`

| Status | `error` |
|--------|---------|
| 400 | `This poll has not started yet.` (new: before `start_at`) |
| 400 | `This poll is closed.` (after `end_at`, closed, cancelled) |
| 400 | `You have already voted for this question.` (same session) |
| 400 | `This question allows only one option (single choice).` |
| 400 | `One or more option_ids are not valid for this question.` |
| 404 | `Question not found.` |

## Admin API (`IsAuthenticated` + `IsAdminUser`, prefix `/api/admin/`)

Errors are always `{"error": "<message>"}` with 400 (validation), 404 (unknown id), 409 (concurrent reset).

| Method | Path | Purpose |
|--------|------|---------|
| GET | `polls/stats/` | Totals (existing keys + `scheduled_polls`, `cancelled_polls`, `ended_polls`, `total_sessions`, `current_session_votes`) |
| GET | `polls/` | List. `search`, `status` (`active`/`scheduled`/`closed`/`cancelled`/`unpublished`), `start_from`, `start_to`, `end_from`, `end_to`, `include_deleted`, `ordering`, `page`, `page_size` |
| POST | `polls/create/` | Create with schedule |
| GET / PUT / PATCH / DELETE | `polls/<id>/` | Detail / update text, type, options, schedule / delete |
| POST | `polls/<id>/activate/` | **New.** Start a scheduled session now, or reopen a closed one (`end_at` required if it has passed) |
| POST | `polls/<id>/close/` | Close; `{"reopen": true, "end_at"?}` reopens |
| POST | `polls/<id>/unpublish/` | Close (legacy alias) |
| POST | `polls/<id>/cancel/` | **New.** Cancel current session (terminal) |
| POST | `polls/<id>/reset/` | **New.** Close current session, keep its votes, open session N+1 |
| GET | `polls/<id>/results/` | **New.** Current session results |
| GET | `polls/<id>/sessions/` | **New.** Paginated session history (newest first), `status` filter |
| GET | `polls/<id>/sessions/<session_id>/` | **New.** Full results of one session |
| POST | `polls/<id>/options/` | Add options (also offered in the open session) |
| DELETE | `polls/<id>/options/<option_id>/` | Soft-delete an option |

Existing admin responses keep all their keys. `options` and `total_votes` now refer to the current session.
New keys on each poll: `status`, `start_at`, `end_at`, `duration_hours`, `created_at`, `updated_at`,
`session_count` and `current_session`.

Edits that would invalidate recorded votes are rejected with 400. These are renaming or removing an option
that has votes in the open session, and changing `question_type` after votes were cast. Changing `start_at`
after the session has started is also rejected. Reset the poll to start a new session instead.

### Create

`POST /api/admin/polls/create/`

```json
{
  "question_text": "Where is EURUSD heading this week?",
  "question_type": "single",
  "options": [{"option_text": "Up"}, {"option_text": "Down"}],
  "start_at": "2026-10-12T08:00:00Z",
  "end_at": "2026-10-19T08:00:00Z"
}
```

Omit `start_at`/`end_at` for "now + 7 days", or send `"duration_days": 3` / `"duration_hours": 12`.

`201`

```json
{
  "message": "Poll created successfully.",
  "question": {
    "id": "3f6c…",
    "question_text": "Where is EURUSD heading this week?",
    "question_type": "single",
    "order": 0,
    "is_active": false,
    "options": [
      {"id": "a1…", "option_text": "Up", "vote_count": 0, "vote_percentage": 0},
      {"id": "b2…", "option_text": "Down", "vote_count": 0, "vote_percentage": 0}
    ],
    "total_votes": 0,
    "status": "scheduled",
    "start_at": "2026-10-12T08:00:00+00:00",
    "end_at": "2026-10-19T08:00:00+00:00",
    "duration_hours": 168,
    "created_at": "2026-10-09T10:00:00+00:00",
    "updated_at": "2026-10-09T10:00:00+00:00",
    "session_count": 1,
    "current_session": {
      "id": "9d0e…",
      "session_number": 1,
      "status": "scheduled",
      "start_at": "2026-10-12T08:00:00+00:00",
      "end_at": "2026-10-19T08:00:00+00:00",
      "created_at": "2026-10-09T10:00:00+00:00",
      "created_reason": "initial",
      "created_by": {"id": "…", "email": "admin@themontada.com"},
      "activated_at": null,
      "closed_at": null,
      "closed_reason": null,
      "closed_by": null,
      "total_votes": 0,
      "options": [
        {"id": "a1…", "option_text": "Up", "vote_count": 0, "vote_percentage": 0},
        {"id": "b2…", "option_text": "Down", "vote_count": 0, "vote_percentage": 0}
      ],
      "total_voters": 0
    }
  }
}
```

### Update schedule

`PATCH /api/admin/polls/<id>/`

```json
{"end_at": "2026-10-21T08:00:00Z"}
```

`duration_days` / `duration_hours` without `end_at` recomputes the end from `start_at`. It also becomes the
default length for future resets.

### Reset

`POST /api/admin/polls/<id>/reset/`

```json
{"duration_days": 7, "current_session_id": "9d0e…"}
```

All fields are optional. `current_session_id` guards against double resets: if someone else already reset
the poll, the request gets `409 {"error": "This poll was already reset. Refresh and try again."}`. A reset
that is already running for the same poll also returns 409.

`201 {"message": "Poll reset. A new voting session has started.", "question": { …, "current_session": {"session_number": 2, "created_reason": "reset", "total_votes": 0, … } }}`

The previous session becomes `closed` with `closed_reason: "reset"`, `closed_at` and `closed_by`. If it had
not started yet it becomes `cancelled` instead.

### History

`GET /api/admin/polls/<id>/sessions/?page=1&page_size=10`

```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "results": [
    {"id": "c3…", "session_number": 2, "status": "active", "total_votes": 1, "total_voters": 1, "options": ["…"], "…": "…"},
    {
      "id": "9d0e…",
      "session_number": 1,
      "status": "closed",
      "start_at": "2026-10-12T08:00:00+00:00",
      "end_at": "2026-10-19T08:00:00+00:00",
      "created_reason": "initial",
      "closed_at": "2026-10-15T12:00:00+00:00",
      "closed_reason": "reset",
      "closed_by": {"id": "…", "email": "admin@themontada.com"},
      "total_votes": 3,
      "total_voters": 3,
      "options": [
        {"id": "a1…", "option_text": "Up", "vote_count": 2, "vote_percentage": 66.67},
        {"id": "b2…", "option_text": "Down", "vote_count": 1, "vote_percentage": 33.33}
      ]
    }
  ],
  "poll": {"id": "3f6c…", "question_text": "…", "question_type": "single", "is_deleted": false, "current_session_id": "c3…", "session_count": 2}
}
```

`total_votes` is the sum of option counts, so a multiple-choice voter counts once per selected option, as
before. `total_voters` is the number of distinct users who voted.

## Scheduler

```bash
python manage.py update_poll_statuses                 # one pass (cron-friendly)
python manage.py update_poll_statuses --loop --interval 60
```

Production runs it under PM2 as `montada-poll-scheduler` (see `ecosystem.config.cjs`):

```bash
pm2 start ecosystem.config.cjs --only montada-poll-scheduler
pm2 save            # persist the process list
pm2 startup         # once per server: restart PM2 + apps after reboot
```

Cron alternative (Linux), if PM2 is not used:

```
* * * * * cd /path/to/Montada && /path/to/env/bin/python manage.py update_poll_statuses >> logs/poll-cron.log 2>&1
```

Transitions and admin actions are logged to `logs/polls.log`.

## Migrations

`0005_poll_sessions_schema` makes additive schema changes only.

`0006_backfill_poll_sessions` gives every existing poll session #1 and links all existing votes to it:
- Active legacy polls stay active with **no end date** (`end_at = null`), so their behaviour does not change.
  Set an end date with PATCH `end_at`, or reset them.
- Inactive legacy polls get a closed session.

`0007_poll_sessions_constraints` replaces the `(user, question, option)` unique constraint with
`(user, session, option)` and makes `PollResponse.session` required. Reversing 0007 fails once any user has
voted in two sessions of the same poll. This is intentional, because the alternative would delete votes.
