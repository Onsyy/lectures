# Lectures

Merged LU + RTU timetable with Discord alerts.

- **Page:** `index.html` shows the week view (GitHub Pages).
- **Checks:** `.github/workflows/timetable.yml` runs `scripts/update.py` every 3 hours,
  plus right away whenever a new LU export is uploaded.
- **Alerts:** changes (time, room, status, new, removed) and an evening message at ~20:00
  with tomorrow's first lecture. Sent to the Discord webhook in the `DISCORD_WEBHOOK` secret.

## Updating LU
Download a fresh export from LU (Excel, "list" format), rename it to `timetable.xlsx`,
and upload it into the `lu` folder (Add file → Upload files) so it replaces the old one.

## New semester
In `scripts/update.py`, update `RTU_SEMESTER_PROGRAM_ID` (find it the same way as before:
nodarbibas.rtu.lv → F12 → Network → findGroupByCourseId) and `SEMESTER_MONTHS`.
