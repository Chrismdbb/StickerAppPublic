# StickerApp

Watches a folder of booth photos and prints + cuts each one on Liene PixCut S1 printers.
A dashboard (http://localhost:8080/) shows the printers, the queue and the history, and
has a Reprint button on every job.

## Run it

Windows (booth PC), from the repo folder, once:

    py -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt

Then double-click `run_stickerapp.bat` (visible window) or `run_stickerapp_hidden.vbs`
(no window; put a shortcut to it in `shell:startup` to run at login).

Try it without a printer (two fake S1s, each sticker takes 25 seconds):

    run_stickerapp.bat --simulate 2

Mac (for testing): `python3 app/main.py --simulate 2 --open`

The first time, open **Settings**, choose the folder your booth software saves photos to,
and press Save. Photos already in that folder are ignored; only new ones print.

## How a photo becomes a sticker

1. A new .jpg/.jpeg/.png appears in the watched folder and stops changing for 2 seconds.
2. It must be 4:7 (1200 x 2100 is ideal). It is scaled to exactly 1200 x 2100.
3. A die-cut path is drawn 20 px outside each person (or a rectangle, in Settings).
   Every photo on the sheet becomes its own sticker.
4. The job goes to the next free printer in rotation (printer 1, 2, 3, back to 1).
   Busy, sleeping or errored printers are skipped.
5. The dashboard shows progress until the printer reports the sheet finished.

## Stopping and clearing

Close the app's window (or press Ctrl+C in it) before deleting `app\data`. Deleting it
while the app runs makes the dashboard say "Lost contact".

## Paper tracking

Each printer card counts the sheets left in its tray (a full load is set in Settings, 15 by
default). The count goes down by one for every finished sticker sheet.

| Sheets left | What the card does |
|---|---|
| more than the "low" number (3 by default) | normal |
| 3 or 2 | yellow glow and a yellow "Low on paper" strip |
| 1 | red flashing glow and a "1 page left" strip |
| 0 | red "Out of paper" strip with a **Paper loaded** button; no jobs are sent to this printer |

Press **Paper loaded** after refilling to set the printer back to a full load. Click the
"N sheets left" text to type in an exact number instead (for example if you loaded 8 sheets).
The app cannot see inside the printer, so the count is only as good as these two buttons.
A sheet is counted when its job finishes, not when a job fails.

## Printer states

| State | Meaning |
|---|---|
| Ready | Idle, will take the next job |
| Busy | Printing |
| Starting | Just woke up (a few seconds) |
| Not found | Asleep or unplugged. Press its power button. |
| Needs attention | A job failed or the printer reported something unfamiliar. Check paper, ink and jams, then press **Resume**. |

The S1 goes to sleep after a while and disappears from USB. Jobs wait in the queue
until a printer wakes up.

## Files it keeps (in `app/data/`, not committed to git)

- `config.json` your settings, printer names and sheet counters
- `jobs.db` job history
- `sheets/` the print-ready sheet and cut file for each job (this is what Reprint uses)
- `thumbs/` previews with the cut line drawn on
- `stickerapp.log` activity log (also shown at the bottom of the dashboard)

History and sheets older than 30 days are removed at startup.

## Known limits

- Tested against real hardware only through the scripts in the repo root (one S1,
  Mac and Windows). The pool logic, restart recovery and failure handling have been
  tested against the simulator; two real printers on one PC have not been tried yet.
- Printer states other than idle (20), starting (30) and busy (40) are shown as
  "unfamiliar" with their raw values, so we can learn what out-of-paper or a jam looks like.
- A job that fails after data has been sent is not retried automatically (it might have
  printed). Use Retry on the dashboard.
- The dashboard listens on this computer only (127.0.0.1).
