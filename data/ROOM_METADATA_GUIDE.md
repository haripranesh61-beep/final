# Room metadata guide

The timetable PDFs establish occupancy and show room labels. They do **not** establish:
- AC / non-AC
- official floor for every room
- capacity
- room type

To make the natural-language search fully exact, fill `room_metadata.json` with official values.

Example:
```json
{
  "name": "G-602",
  "floor": "Ground",
  "ac": true,
  "capacity": 60,
  "room_type": "classroom",
  "verified": true,
  "verification_note": "Verified from official room list",
  "ac_note": "Verified from official room list",
  "source": "official room inventory"
}
```

Do not set `verified: true` unless the value has been confirmed by the organizers/official room inventory.
