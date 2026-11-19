# Trade Ideas: three targets + stop-loss move alerts (mobile changelog)

Nothing existing was renamed or removed. Older app builds keep working unchanged.

> Status: implemented and unit-tested locally. **Not yet deployed or curl-checked on UAT** — run the
> curl examples below on UAT after `migrate` and paste the real responses here before prod.

## A. Target prices

| Meaning | Key | Notes |
|---|---|---|
| Target 1 | `take_profit` | unchanged, still required |
| Target 2 | `take_profit_2` | new, optional, `null` when unset |
| Target 3 | `take_profit_3` | new, optional, `null` when unset |

Accepted on `POST /api/signals/create/` and `PUT|PATCH /api/signals/edit-my-signals/{id}/`
(the edit route accepts PUT/PATCH, as before — not POST).
Returned (always present, `null` when unset) everywhere the trade idea is returned: create/edit
responses, `my-signals`, `view-signals` (list + detail), analyst signals feed, applied signals.

Validation (HTTP 400, errors keyed by field, same shape as today's `take_profit` errors):

- `take_profit_2` needs `take_profit`; `take_profit_3` needs `take_profit_2`.
- BUY: `entry_price < take_profit < take_profit_2 < take_profit_3`; SELL: strictly descending.
- Values must be > 0.
- `status = DRAFT` skips the ordering checks (chain checks still apply).
- On edit the stored values count: sending only `take_profit_3` on a signal without TP2 is rejected.
  To remove TP2, send `take_profit_2: null` (and `take_profit_3: null` if set).

Request:

```json
{ "asset_class": "<uuid>", "instrument": "<uuid>", "direction": "BUY",
  "entry_price": "1.08500", "stop_loss": "1.08000",
  "take_profit": "1.09500", "take_profit_2": "1.10000", "take_profit_3": "1.11000",
  "timeframe": "<id>", "confidence_level": 80, "status": "OPEN" }
```

Error example:

```json
{ "take_profit_2": "For a BUY signal, take_profit_2 must be above take_profit." }
```

## B. New notify categories (stop-loss moves)

`POST /api/signals/{signal_id}/notify/` — body unchanged (`audience`, `category`, `custom_title`, `custom_body`).

| `category` | New stop loss |
|---|---|
| `move_sl_to_entry` | `entry_price` |
| `move_sl_to_tp1` | `take_profit` |
| `move_sl_to_tp2` | `take_profit_2` |
| `move_sl_to_tp3` | `take_profit_3` |

Behaviour:

- The stored `stop_loss` is updated and the old value is saved in `SignalStopLossChange`
  (signal, category, previous_stop_loss, new_stop_loss, analyst, created_at).
- 400 with `{"error": "..."}`: signal is CLOSED, signal is DRAFT, or the chosen target is `null`.
  404: not the caller's signal. 409: the signal changed/closed while the request ran (retry after refresh).
- The stop is updated even when there are no recipients (response then has `recipient_count: 0`).
- 200 response = the usual notify response **plus** `"signal"` (full signal object, same as `view-signals/{id}/`).
  Other categories do not return `signal`.

Request:

```json
{ "audience": "followers", "category": "move_sl_to_entry", "custom_title": "", "custom_body": "" }
```

Response (200):

```json
{
  "message": "Stop loss updated and notification queued.",
  "audience": "followers", "category": "move_sl_to_entry",
  "title": "EURUSD: Move stop loss to entry",
  "body": "Analyst Ann Analyst moved the stop loss to 1.08500 (entry). Adjust your trade.",
  "recipient_count": 12, "device_tokens_found": 18, "success_count": 0, "failure_count": 0,
  "signal": { "id": "<uuid>", "stop_loss": "1.08500", "take_profit": "1.09500", "take_profit_2": "1.10000", "...": "..." }
}
```

FCM `data` (all strings; `sent_at`, `silent`, `sound`, `source`, `preference_category` are added by the push layer as for every push):

```json
{
  "type": "signal_alert",
  "signal_id": "<uuid>", "trade_idea_id": "<uuid>",
  "category": "move_sl_to_entry",
  "notification_id": "<uuid, new for every push>",
  "symbol": "EURUSD", "direction": "BUY",
  "previous_stop_loss": "1.08000", "new_stop_loss": "1.08500", "stop_loss": "1.08500",
  "entry_price": "1.08500", "take_profit": "1.09500", "take_profit_2": "1.10000", "take_profit_3": "",
  "timeframe": "H1", "signal_status": "OPEN", "status": "OPEN"
}
```

The same title/body are in the FCM `notification` block and saved to the in-app notification list
(category `TRADING_SIGNAL`), like the existing signal alerts.

## Differences from the request — please confirm

1. **`title`/`body` are not in FCM `data`.** `push_contract.build_data` strips them on purpose
   (`PUSH_DATA_INCLUDE_TEXT` is off) because the released app draws a second local notification when it
   finds them. They are in the `notification` block. Enable the setting only if the app build handles it.
2. **iOS sound** follows the push contract (`montada_push.wav`, same as every signal push), not `"default"`.
3. **No EN/AR localization:** the backend does not localize signal pushes today (English only).
4. Recipients: same as other notify categories, plus followers who muted the analyst's trade ideas
   (analyst profile bell) are now skipped for all notify categories.

## For the product team: where TP2/TP3 do NOT affect outcomes yet (unchanged on purpose)

- `run_price_alerts` closes a signal and decides win/loss on `take_profit` (TP1) / `stop_loss` only.
  Reaching TP2/TP3 does nothing. A stop moved to TP1/TP2/TP3 or entry is then hit as an "SL" close —
  decide whether that counts as loss, win or neutral (a stop at/above entry on a BUY is arguably not a loss).
- Admin analytics (`MontadaAdmin/views.py`, risk/reward using `abs(take_profit - entry_price)`) use TP1.
- Trader-side applied signals and price-move alerts use TP1 only.
- Moving the stop does not reset an already-sent TP/SL hit flag (`price_alert_fcm_sent`).

## Deploy / verify on UAT

```bash
python manage.py migrate        # Signals 0016: take_profit_2/3 + SignalStopLossChange
TOKEN=<analyst jwt>; BASE=https://uat.themontada.com

curl -s -X POST $BASE/api/signals/create/ -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"asset_class":"<uuid>","instrument":"<uuid>","direction":"BUY","entry_price":"1.08500","stop_loss":"1.08000","take_profit":"1.09500","take_profit_2":"1.10000","take_profit_3":"1.11000","timeframe":"<id>","confidence_level":80,"status":"OPEN"}'

curl -s -X POST $BASE/api/signals/<signal_id>/notify/ -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"audience":"followers","category":"move_sl_to_tp1"}'
```

Note: `**/migrations/*.py` is git-ignored — copy `Signals/migrations/0016_*.py` to UAT/prod explicitly.
