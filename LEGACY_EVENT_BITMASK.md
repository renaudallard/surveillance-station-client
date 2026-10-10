# Legacy Event Bitmask Reference

Reverse-engineered documentation of Synology Surveillance Station's
undocumented `event_map` flag bitmask, as the app's `LegacyEventBackend`
decodes it. Surveillance Station 9.3 added the Event Center API, which
returns typed events instead; on 9.3 the `event_map` bits also no longer
follow the table below (`reserved` carries a full bitmask there, not just
0 or 1). This is a living reference —
contributors testing new camera brands/models are encouraged to add their
findings here, following the format and confirmation standards below.

## Goal

Keep `src/surveillance/data/legacy_event_bits.json` — the bit table the app
actually decodes events with (via `src/surveillance/services/legacy_event_bits.py`)
— accurate, sourced from live testing rather than guessing. This doc is the
narrative/methodology companion to that JSON, not a duplicate of its data:
see [Bit table](#bit-table) below for how the two relate.

## Background

`src/surveillance/services/legacy_event.py` decodes `RecordingPicker::EnumInterval`'s
`event_map` — a run-length-encoded array of `[value, flag, reserved]` tuples,
each meaning `value * 5` seconds in state `flag`. `flag` is a **signed
32-bit bitmask** (bit 31 shows up as a negative number — recover the
unsigned bitmask with `unsigned = value + 2**32`). Neither
`RecordingPicker::EnumInterval` nor this bitmask appears anywhere in
Synology's official Web API PDF (v3.11) or any findable open-source
Synology client (py-synology, SynologyIPCAM, openHAB/Homey, SynoAI) — it's
undocumented and, as far as we can tell, unreverse-engineered anywhere
public.

**Confirmation methods, most to least reliable:**
1. `log.db`'s `eventlog` table (see [Reference](#reference)) — exact
   timestamped, human-readable category per camera, cross-referenced
   against `event_map` for the same window. The gold standard.
2. DSM's live Monitor Center → Event filter — shows the category DSM
   assigned to a real triggered event, without DB access.
3. Action Rule wizard's Event/Application dropdown — shows official
   category *names* for a camera, but doesn't require a real event. **Only
   a source of vocabulary, not confirmation** — matching a flag to a
   category name this way is a guess, not a fact. An early mapping attempt
   did exactly this and got the wrong category (a flag guessed as "Scene
   Change Detection" from its name turned out to be "Motion detected");
   treat name-only matches as unconfirmed until checked via method 1 or 2.

**Key gotchas:**
- `eventlog` only logs a category's *new* trigger, not its continuing
  state. If a class was already active in the previous 5-second bucket, a
  bucket where it's still present but nothing new fired can be completely
  silent in `eventlog` even though the bit is genuinely set in `event_map`.
  Always check the surrounding buckets, not just the one in question,
  before treating a "silent" `eventlog` window as a contradiction.
- A flag can occasionally lack a companion bit you'd otherwise expect
  (e.g. a class detection with obvious motion but no bit 1/8) — this is
  more likely a bucket-boundary artifact of the 5-second RLE granularity
  than a real exception. Check the immediately adjacent buckets before
  concluding a bit's meaning is wrong.
- **Bit meanings are not universal across camera brands** — DSM assigns
  class-bit positions per brand at ingest, not globally. Confirmed
  collisions exist on bits 25 and 27 (see table below). Don't assume a
  meaning found on one brand/model applies to another without checking.

## Camera models tested

| Brand | Model | Notes |
|---|---|---|
| Vivotek | VC8101 | |
| D-Link | DCS-4622 | |
| HIKVISION | DS-2CD2T42WD-I5 | |
| HIKVISION | DS-2CD2387G2-LSU/SL | |
| Reolink | RLC-823A | PTZ, AI-capable |
| HIKVISION | DS-2CD2042WD-I | |
| HIKVISION | DS-2CD2542FWD-IS | |
| HIKVISION | DS-2CD2342WD-I | |
| HIKVISION | DS-2CD2185FWD-IS | |
| (generic/manual RTSP source) | e.g. a dashcam | DSM has no model profile for an unrecognized/manual source — it offers only its generic fallback event set (Motion, Live view analytics, Connection lost/normal, Camera enabled/disabled, QR code), not real camera capability |

DSM assigns each camera an internal `camera_id` (used in `event_map` and
`eventlog.device_id`) when it's added — this id does **not** correspond to
the camera's display number or add-order and must be looked up per
installation (e.g. via the app's own camera list), not assumed.

## Bit table

Bit meanings live in `src/surveillance/data/legacy_event_bits.json` — the single
source of truth, loaded directly by `src/surveillance/services/legacy_event_bits.py`
to decode real events. Don't duplicate its contents here; update the JSON
(following [Contributing](#contributing) below) and let this doc stay
narrative.

Schema: a `bits` map keyed by bit number as a string (`"0"`-`"31"`, or
`"reserved:0"` for the 3rd RLE field), each value a list of variant
objects — one per brand the bit's meaning differs for, or a single
`"brands": ["*"]` entry when it's universal:

```json
"25": [
  {"brands": ["reolink"],   "label": "Person Detect",          "confirmed": true, "notes": "..."},
  {"brands": ["hikvision"], "label": "Scene Change Detection", "confirmed": true, "notes": "..."}
]
```

`confirmed` is a boolean; the confirmed/strongly-inferred/proposed nuance
this doc used to track separately now lives entirely in each variant's
free-text `notes`. Bits 0 and 1 are present in the JSON for completeness
but are modifier bits, not detected categories — the app never surfaces
them as a filter option (see bit 1's entry under Open questions below).

**Notable pattern, worth knowing before editing the JSON:** bits 24-31 on
Hikvision line up exactly with that camera's own Smart Event menu order
(Defocus=24, Scene Change=25, Face Detection=26, Intrusion=27, Line
Crossing=28, Region Entrance=29, Region Exiting=30, Unattended Baggage=31),
then Object Removal Detection (the menu's 9th item) has no bit left and
overflows into `reserved` instead. Bits 25 and 27-31 are independently
`eventlog`-confirmed; 24 and 26 fit the pattern with no gaps but aren't
directly confirmed, hence `"confirmed": false` in the JSON. The menu
continues past Object Removal Detection with Temperature Measurement and
Face Temperature Measurement (thermal-capable models only, untested) — if
the pattern holds, those would need further capacity beyond the single
`reserved` bit observed so far (e.g. `reserved` itself being a small
bitmask, not just a boolean).

## Known event categories not yet mapped to a bit

Every category name seen so far in a camera's Advanced/Smart Event menu,
the Action Rule Event dropdown, or DSM's generic baseline event set, that
doesn't have a confirmed (or strongly inferred) bit yet. Listed here so
gaps stay visible instead of silently dropped from the doc.

| Category | Seen on | Notes |
|---|---|---|
| Smart Motion Detection | Hikvision (menu entry) | Not offered as supported on any tested Hikvision model |
| Loitering Detection | Hikvision (menu entry) | Not offered as supported on any tested model |
| Crowd Detection | Hikvision (menu entry) | Not offered as supported on any tested model |
| Missing Object Detection | Hikvision (menu entry) | Not offered as supported on any tested model |
| Unattended Object Detection | Hikvision (menu entry) | Not offered as supported on any tested model |
| Running Detection | Hikvision (menu entry) | Not offered as supported on any tested model |
| Temperature Measurement | Hikvision (menu entry, thermal models only) | Comes after Object Removal Detection in the menu, past the `reserved`-field overflow point already in use — see the bit 24-31 note above |
| Face Temperature Measurement | Hikvision (menu entry, thermal models only) | Same as above |
| Digital input (inactive) | all brands with digital input | Only the active direction has been triggered so far |
| Image too dark / too bright / too blurry detection | Vivotek (native Tampering sub-checkboxes) | Likely collapse into the single Tampering bit (10) in DSM's own Action Rule view, but none fired during testing to confirm |
| Live view analytics detected | generic baseline, all brands | DSM's own category; unclear if/how it's distinct from Motion detected (bit 8) at the `event_map` level |
| Advanced event detected (no sub-type) | cameras whose Advanced Event menu isn't broken into named sub-types | Generic bucket, not cross-referenced |
| Connection lost / Connection normal | generic baseline, all brands | Offered as an Action Rule trigger, but a real disconnect/reconnect produced no matching entry in Events or Notifications during testing — may need enabling elsewhere first |
| Camera enabled / Camera disabled | generic baseline, all brands | Same caveat — toggling a camera on/off produced nothing in Events or Notifications |
| Occupancy below threshold | most brands | |
| QR code detected | generic baseline, all brands | Previously misattributed to bit 10 (corrected to Tampering detected) — real flag still unknown |
| PIR Motion, Smoke, Face Recognition, Bookmark, Action rule, Live view alert, Edge recording, Transaction Device | seen in the Action Rule Event dropdown | Not yet cross-referenced to any camera's Advanced/Smart Event submenu, so it's unclear which brand(s) actually offer each one |

## Decoding a flag by hand

A flag is just the sum of its set bits' values — with the bit table above
(now in `legacy_event_bits.json`) as the only other ingredient, any flag decodes
the same way. Worked examples, since a flag-value table would just be
redundant derived data that drifts out of sync with the JSON:

- **33554435** = 33554432 (bit 25) + 2 (bit 1) + 1 (bit 0) → bits {0, 1,
  25}. Bit 25 is brand-dependent: **Person Detect** on Reolink, **Scene
  Change Detection** on Hikvision — the confirmed brand collision.
- **771** = 512 (bit 9) + 256 (bit 8) + 2 (bit 1) + 1 (bit 0) → bits {0, 1,
  8, 9} → **Motion detected + Audio detected** (both universal bits, no
  brand lookup needed).
- **-2147483647**: negative because bit 31 is the sign bit in this signed
  32-bit field. Recover the unsigned value first: `-2147483647 + 2**32 =
  2147483649` = 2³¹ + 1 → bits {0, 31} → **Unattended Baggage Detection**
  (Hikvision).
- A flag with only bits {0, 1} set (e.g. `3`) decodes to no real category —
  `decode_legacy_flag()` returns an empty list for this case (the app's own
  `_create_event_row` then falls back to `"Unclassified"`), though
  `LegacyEventBackend.list_events()` normally filters flags 0/1 out entirely before
  they'd reach that path.

## Open questions

- Bit 1 — Surveillance Station's own `REC_TRIG_LABEL` table (EventCommon.js)
  names it advanced continuous recording, but that hasn't been checked
  against each camera's recording settings yet.
- Bit 24 — Face Detect (Reolink) proposed but never observed; Defocus
  Detection (Hikvision) strongly inferred from the sequential menu-order
  pattern but never directly triggered/cross-referenced. Neither is
  `eventlog`-confirmed.
- Bit 26 — Face Detection (Hikvision) strongly inferred the same way, one
  real sample plus the pattern fit, but not independently confirmed the
  way bits 25/27 are.
- `reserved` field — only ever observed as 0 or 1; unknown if other values
  exist (see Temperature/Face Temperature Measurement note above).
- See [Known event categories not yet mapped to a bit](#known-event-categories-not-yet-mapped-to-a-bit)
  for the full list of category names with no bit yet, including the
  Connection lost/restored, Camera enabled/disabled, and Vivotek Tampering
  sub-condition caveats.

## Reference

- **Gathering more samples:** DSM web UI → Monitor Center → timeline →
  funnel icon → Event filter (live, real-event category name); or Action
  Rule wizard → Add → Event: "Advanced event detected" → Application
  dropdown (vocabulary only, not confirmation — see Background).
- **DB route** (read-only via SSH, key-based; `sudo` on the NAS needs an
  interactive password, so run queries directly on the NAS or via an
  account with `sudo` access). Both DBs live under `@surveillance`'s data
  directory on whichever volume Surveillance Station was installed to
  (commonly `/volume1/@surveillance/`, but check yours — DSM's Package
  Center lets you pick the volume at install time):
  - **`log.db`, table `eventlog`** — the ground-truth source. Columns:
    `device_id` (= app camera id), `start_time` (unix epoch), `type` (`5`=
    Motion detected, `7`=Digital input, `10`=Audio detected, `11`=
    Tampering detected, `13`=Advanced event detected, others unmapped),
    `device_name`, `description` (human-readable, e.g. "Advanced event
    detected: Object Removal Detection"), `paired_cam_id`.
    ```sql
    SELECT device_id, start_time, type, device_name, description
    FROM eventlog WHERE start_time BETWEEN <from> AND <to> ORDER BY start_time;
    ```
    Other `log.db` tables checked and not useful: `synocam_log` (0 rows
    despite a promising schema), `switch_event` (0 rows), `log` (only
    playback-history entries).
  - `detection_event.db` — checked and not useful. `detection_event_video_
    analysis`, `detection_event_crowd`, `detection_event_smoke`,
    `detection_event_license_plate` are all empty NAS-wide.
    `detection_event_motion` is populated but redundant with `eventlog`.
- The app decodes events directly from `legacy_event_bits.json` via
  `src/surveillance/services/legacy_event_bits.py` (`decode_legacy_flag()`,
  behind `LegacyEventBackend`) — keyed by camera brand as well as flag, per the bit
  27/25 collisions above. There's no separate in-code table to keep in
  sync; editing the JSON is sufficient.
- **Vendor protocol check, don't redo:** neither Hikvision's ISAPI
  (`linedetection`, `fielddetection`, `regionEntrance`, `tamperdetection`,
  etc.) nor Reolink's API (`people`, `vehicle`, `dog_cat`) expose a numeric
  bitmask — both report named strings. DSM itself must be assigning bit
  positions on ingest, consistent with the confirmed per-brand collisions.

## Contributing

### Procedure

1. **Enable every event on the camera(s) you're testing.** DSM web UI → IP
   Camera → double-click the camera → **Event Detection** → the
   **Advanced Event (Smart Event)** tab → tick "Enable on Surveillance
   Station" for every row marked `Supported: Yes`. Also check the
   **Motion**, **Audio**, and **Tampering** tabs if those bits aren't
   already covered for this brand.
2. **Trigger as many of them for real as practical** (walk through the
   scene, cross a line, linger, remove/leave an object, cover the lens,
   etc.) and note the approximate start/end time — a few minutes of
   activity is enough, the next step re-derives exact timing.
3. **Dump the raw `event_map` for that window** using
   [`scripts/dump_event_map.py`](scripts/dump_event_map.py) (uses this
   app's own config/credentials, so run it from a checkout that's already
   logged in via the GUI at least once):
   ```sh
   python3 scripts/dump_event_map.py "<camera name substring>[,<camera2>]" \
       --date 2026-08-01 --from 20:12:00 --to 20:26:00
   ```
   This prints each camera's internal `camera_id`, then every 5-second
   bucket in that window whose `flag` isn't just 0/1 — bucket start/end
   time, `start_epoch`, `flag`, and `reserved`. Pass `--all` to also see
   the quiet buckets. Cameras are matched by substring against their
   display name; `--profile` overrides the app's default connection
   profile if you have more than one configured.
4. **For every new or ambiguous flag**, note its `camera_id` and the
   bucket's `start_epoch`/end epoch from the output above.
5. **Cross-reference against `eventlog`** (see [Reference](#reference) for
   the DB path and access details) — combine every window from step 4
   into one query:
   ```sh
   sudo sh -c '
   DB=/path/to/@surveillance/log.db
   sqlite3 -header -column $DB "SELECT device_id,start_time,type,device_name,description
   FROM eventlog WHERE device_id IN (<id1>,<id2>) AND (
     (start_time BETWEEN <from1> AND <to1>) OR
     (start_time BETWEEN <from2> AND <to2>)
   ) ORDER BY start_time;"
   '
   ```
   Match each `eventlog` row's `start_time` against the bucket ranges from
   step 4 — a row landing inside a bucket confirms what that bucket's
   `flag` means. Remember the "silent bucket" gotcha above: a bit can be
   genuinely set with no matching `eventlog` row if that class was already
   active in the previous bucket.
6. **Decode the flag's bits** to know which ones to update:
   ```sh
   python3 -c "
   import sys; sys.path.insert(0, 'src')
   from surveillance.services.legacy_event_bits import decode_legacy_flag
   print(decode_legacy_flag(<flag>, <reserved>, '<brand>'))
   "
   ```
   (handles the negative-flag/sign-bit recovery internally — see
   `decode_legacy_flag()` in `src/surveillance/services/legacy_event_bits.py` if you want
   the raw bit list instead: `[i for i in range(32) if (v & 0xFFFFFFFF) & (1 << i)]`).
7. **Update `src/surveillance/data/legacy_event_bits.json`**: add/adjust the
   variant(s) for the bit(s) you confirmed, and remove the matching entry
   from this doc's Known event categories not yet mapped table if it
   covered the same category.

### What to include

When you observe a new event category or a colliding bit meaning on a
camera brand/model not yet in `legacy_event_bits.json`, add it there with:
- Brand and model.
- The confirmation method used (`eventlog` cross-reference preferred; a
  live Monitor Center match is acceptable; an Action Rule dropdown name
  match alone is not — see Confirmation methods above).

Don't assume a bit's meaning found on one brand applies to another —
verify per brand/model given the confirmed collisions on bits 25 and 27.
