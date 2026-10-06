# Lectures

Merged LU + RTU timetable with Discord alerts.

- **Page:** `index.html` shows the week view (GitHub Pages).
- **Checks:** `.github/workflows/timetable.yml` runs `scripts/update.py` every 3 hours,
  for both universities.
- **Alerts:** changes (time, room, status, new, removed) and an evening message at ~20:00
  with tomorrow's first lecture. Sent to the Discord webhook in the `DISCORD_WEBHOOK` secret.

## Sources
- RTU: nodarbibas.rtu.lv (public data request), group set by `RTU_SEMESTER_PROGRAM_ID`.
- LU: lekciju-saraksts.lu.lv public group page, set by `LU_URL`.
Both are checked automatically every 3 hours.

## New semester
In `scripts/update.py`, update `RTU_SEMESTER_PROGRAM_ID` (find it the same way as before:
nodarbibas.rtu.lv → F12 → Network → findGroupByCourseId) and `SEMESTER_MONTHS`, and point `LU_URL` at the new LU group page.
